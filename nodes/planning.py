"""
PLANNING LAYER — Spec-to-Task Converter
----------------------------------------
Читает user-spec.md, вызывает Qwen-Max API, генерирует TaskInput.

ВАЖНО: TaskInput НЕ содержит acceptance_criteria, tests, rubric.
Эти поля добавляются позже в Verification (изолированно от Execution).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))
from contracts import TaskInput, seal_task_for_execution

console = Console()

# Используем Ollama локально
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b-instruct")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")


def _get_ollama_model() -> str:
    """Возвращает название модели Ollama."""
    return DEFAULT_OLLAMA_MODEL


def _is_no_endpoints_error(exc: Exception) -> bool:
    return "No endpoints found for" in str(exc)


# ─── Промпт для Planning (генерирует ТОЛЬКО техническое описание) ─────────────
SYSTEM_PROMPT = """You are a technical planning agent. You receive a user specification (feature description) and must generate a single atomic task for implementation.

Your output MUST be a JSON object with these fields:
- task_id: string (format: "task-XXX")
- description: string (technical description of what to implement, NO acceptance criteria, NO test cases)
- context_files: list of strings (paths to existing files that should be read for context)
- language: string ("python", "javascript", "typescript", "php", etc)
- target_path: string (where the new code should be written)

CRITICAL RULES:
1. Description must be TECHNICAL (how to implement), not BEHAVIORAL (what tests will check)
2. Do NOT include acceptance criteria, test cases, or rubric in description
3. Context files should be existing files in the project (for style/pattern reference)
4. Target path should follow project conventions

Example output:
```json
{
  "task_id": "task-001",
  "description": "Create a Python module with function `is_palindrome(s: str) -> bool` that normalizes input (lowercase, remove non-alphanumeric) and checks if it reads same forwards and backwards. Use type hints and docstring.",
  "context_files": [],
  "language": "python",
  "target_path": "src/utils/palindrome.py"
}
```
Output ONLY the JSON object, no markdown, no explanation."""


async def plan(user_spec_path: Path, feature_name: str | None = None) -> TaskInput:
    """
    Читает user-spec.md и генерирует TaskInput через Qwen-Max API.

    Args:
        user_spec_path: Путь к user-spec.md
        feature_name: Название фичи (для task_id)

    Returns:
        TaskInput (запечатанный, без критериев)
    """
    if not user_spec_path.exists():
        raise FileNotFoundError(f"User spec not found: {user_spec_path}")

    user_spec = user_spec_path.read_text()
    feature_name = feature_name or user_spec_path.parent.name

    console.print(f"\n[bold cyan]═══ PLANNING: {feature_name} ═══[/]\n")
    console.print(f"📄 Reading: {user_spec_path}")
    console.print(f"📝 Spec length: {len(user_spec)} chars\n")

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=f"User Specification:\n\n{user_spec}"),
    ]
    model_name = _get_ollama_model()

    console.print("🧠 Calling Ollama...")
    console.print(f"🤖 Model: {model_name}")

    llm = ChatOpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",  # Ollama не требует реальный API key
        model=model_name,
        temperature=0.3,
        max_tokens=1024,
    )

    try:
        console.print(f"→ Using model: [bold]{model_name}[/]")
        response = await asyncio.to_thread(llm.invoke, messages)
        raw_output = response.content.strip()
        console.print(f"✅ Response from {model_name}: {len(raw_output)} chars\n")
    except Exception as e:
        console.print(f"[red]❌ Ollama call failed: {e}[/]")
        raise

    # Парсинг JSON из ответа
    try:
        # Убираем markdown если есть
        if raw_output.startswith("```json"):
            raw_output = raw_output[7:]
        if raw_output.startswith("```"):
            raw_output = raw_output[3:]
        if raw_output.endswith("```"):
            raw_output = raw_output[:-3]

        task_dict = json.loads(raw_output.strip())
    except json.JSONDecodeError as e:
        console.print(f"[red]❌ JSON parse error: {e}[/]")
        console.print(f"Raw output:\n{raw_output[:500]}")
        raise

    # Валидация и запечатывание через contracts.py
    console.print("🔒 Sealing task (removing forbidden fields)...")
    sealed_task = seal_task_for_execution(task_dict)

    console.print("\n[bold green]═══ TASK GENERATED ═══[/]")
    console.print(f"ID: {sealed_task['task_id']}")
    console.print(f"Language: {sealed_task['language']}")
    console.print(f"Target: {sealed_task['target_path']}")
    console.print(f"Context files: {len(sealed_task['context_files'])}")
    console.print("\nDescription preview:")
    console.print(f"  {sealed_task['description'][:200]}...")

    # Проверяем что запрещённые поля отсутствуют
    forbidden = {"acceptance_criteria", "test_cases", "rubric", "worker_id"}
    leaked = forbidden & set(sealed_task.keys())
    if leaked:
        console.print(f"\n[bold red]🚫 GOODHART VIOLATION: {leaked}[/]")
        raise ValueError(f"Planning leaked forbidden fields: {leaked}")

    console.print("\n[green]✅ Goodhart-proof: no forbidden fields[/]")
    return sealed_task


# ─── Изолированный тест ─────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    # Проверяем что Ollama доступна
    import requests

    try:
        response = requests.get(
            f"{OLLAMA_BASE_URL.replace('/v1', '')}/api/tags", timeout=2
        )
        if response.status_code != 200:
            console.print("[red]❌ Ollama not responding[/]")
            console.print("Start Ollama: ollama serve")
            raise SystemExit(1)
    except requests.exceptions.RequestException:
        console.print("[red]❌ Cannot connect to Ollama[/]")
        console.print(f"Check that Ollama is running on {OLLAMA_BASE_URL}")
        raise SystemExit(1)

    # Тестовый user-spec
    test_spec = Path("work/mvp/user-spec.md")
    if not test_spec.exists():
        console.print(f"[red]❌ Test spec not found: {test_spec}[/]")
        console.print("Create it with:")
        console.print("  mkdir -p work/mvp")
        console.print("  cat > work/mvp/user-spec.md << 'EOF'")
        console.print("# Feature: Calculator Module")
        console.print("")
        console.print("## Description")
        console.print(
            "Create a simple Python module that provides basic arithmetic operations."
        )
        console.print("")
        console.print("## Goals")
        console.print("- Implement add, subtract, multiply, divide functions")
        console.print("- Handle division by zero gracefully")
        console.print("- Include type hints")
        console.print("")
        console.print("## Constraints")
        console.print("- Python 3.11+")
        console.print("- No external dependencies")
        console.print("- Pure functions only")
        console.print("EOF")
        raise SystemExit(1)

    console.print("\n[bold magenta]═══ PLANNING NODE TEST ═══[/]\n")

    try:
        task = asyncio.run(plan(test_spec))

        # Сохраняем результат
        output_path = Path("work/mvp/results/task-input.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(task, indent=2))
        console.print(f"\n💾 Saved to: {output_path}")
    except Exception as e:
        console.print(f"[red]❌ Planning failed: {e}[/]")
        import traceback

        traceback.print_exc()
