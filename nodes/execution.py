"""
EXECUTION LAYER — Goodhart-Proof Code Generator
-----------------------------------------------
Получает ТОЛЬКО описание задачи и контекстные файлы.
НЕ получает: acceptance_criteria, tests, rubric, worker_id.

Возвращает SealedArtifact (git diff + logs) без worker_id.
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from rich.console import Console

FORCE_BAD_CODE = os.getenv("FORCE_BAD_CODE", "false").lower() == "true"

sys.path.insert(0, str(Path(__file__).parent.parent))
from contracts import CodeArtifact, TaskInput, seal_artifact_for_verification

console = Console()

# ─── Промпты (без упоминания тестов или критериев!) ─────────────────────────
SYSTEM_PROMPT = """You are a precise code generator. You receive:
1. A task description (what needs to be implemented)
2. Context files from the codebase (for style/structure reference)
3. A target path where the new code should be placed

Your job:
- Write clean, idiomatic code for the specified language
- Follow patterns from context files
- Include type hints where appropriate
- Handle errors gracefully
- Output ONLY the code, wrapped in markdown code blocks with language tag

Output format:
```language:path/to/file.py
<code here>
```

If multiple files are needed, output multiple code blocks.
Do NOT write tests. Do NOT write explanations outside code blocks."""


def _build_user_prompt(task: TaskInput, context_contents: dict[str, str]) -> str:
    """
    Формирует промпт для воркера.
    ⛔ Здесь НЕТ ни слова о тестах, критериях, рубриках.
    """
    parts = [f"# Task\n{task['description']}\n"]
    parts.append(f"# Language\n{task['language']}\n")
    parts.append(f"# Target Path\n{task['target_path']}\n")

    if context_contents:
        parts.append("# Context Files\n")
        for path, content in context_contents.items():
            parts.append(f"## {path}\n```{task['language']}\n{content}\n```\n")

    return "\n".join(parts)


def _read_context_files(files: list[str]) -> dict[str, str]:
    """Читает файлы контекста, игнорируя отсутствующие."""
    contents: dict[str, str] = {}
    for path in files:
        p = Path(path)
        if p.exists() and p.is_file():
            try:
                contents[str(p)] = p.read_text()
            except Exception as e:
                console.print(f"[yellow]⚠ Cannot read {p}: {e}[/]")
        else:
            console.print(f"[yellow]⚠ Context file not found: {p}[/]")
    return contents


def _sanitize_generated_path(candidate_path: str, fallback_path: str) -> str:
    """
    Нормализует путь, полученный от LLM.

    Если модель вернула комментарий, абсолютный путь, traversal или слишком
    длинное имя файла, используем fallback_path вместо сырого значения.
    """
    candidate = candidate_path.strip().strip("`'\"")
    candidate = candidate.replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]

    if not candidate or candidate.startswith("/") or len(candidate) > 180:
        return fallback_path

    if re.fullmatch(r"[A-Za-z0-9_./-]+", candidate) is None:
        return fallback_path

    parts = PurePosixPath(candidate).parts
    if any(part in ("", ".", "..") for part in parts):
        return fallback_path

    return candidate


def _extract_text_content(content: Any) -> str:
    """Нормализует content из ответа LangChain/OpenAI-подобных клиентов в строку."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    return str(content)


def _parse_code_blocks(
    response: str, fallback_path: str = "generated.py"
) -> dict[str, str]:
    """
    Извлекает файлы из ответа LLM. Покрывает 6 форматов:
    1. ```python:path/to/file.py      (идеальный)
    2. # path/to/file.py перед блоком (Qwen с комментариями)
    3. // path/to/file.py перед блоком (JS/TS)
    4. path/to/file.py перед блоком БЕЗ комментария (Qwen без #)  ← НОВЫЙ
    5. Просто ```python (fallback на target_path)
    6. Множественные блоки
    """
    files: dict[str, str] = {}

    # Формат 1: ```lang:path
    pattern1 = r"```(\w+):([^\n`]+\.\w+)\n(.*?)```"
    for _, path, code in re.findall(pattern1, response, re.DOTALL):
        safe_path = _sanitize_generated_path(path, fallback_path)
        files[safe_path] = code.strip()
    if files:
        return files

    # Формат 2 и 3: # или // перед блоком
    pattern2 = r"(?:^|\n)(?:#|//)\s*([^\n`]+\.\w+)\s*\n```(\w+)\n(.*?)```"
    for path, _, code in re.findall(pattern2, response, re.DOTALL):
        safe_path = _sanitize_generated_path(path, fallback_path)
        files[safe_path] = code.strip()
    if files:
        return files

    # Формат 4: путь БЕЗ комментария перед ```
    pattern3 = r"(?:^|\n)\s*([A-Za-z0-9_./\\-]+\.\w{1,5})\s*\n```(\w+)\n(.*?)```"
    for path, _, code in re.findall(pattern3, response, re.DOTALL):
        if "/" in path or path.count(".") >= 1:
            safe_path = _sanitize_generated_path(path, fallback_path)
            files[safe_path] = code.strip()
    if files:
        return files

    # Формат 5: один блок без пути — используем fallback
    pattern4 = r"```(\w+)\n(.*?)```"
    matches = re.findall(pattern4, response, re.DOTALL)

    if len(matches) == 1:
        files[fallback_path] = matches[0][1].strip()
    elif len(matches) > 1:
        for i, (lang, code) in enumerate(matches, 1):
            ext = {
                "python": "py",
                "javascript": "js",
                "typescript": "ts",
                "php": "php",
                "bash": "sh",
                "ruby": "rb",
            }.get(lang, lang)
            files[f"generated_{i}.{ext}"] = code.strip()

    return files


def _strip_path_echo(code: str, filepath: str) -> str:
    """
    Защитный слой: если LLM эхом продублировала путь в первой строке кода —
    вырезаем её. Это частый баг Qwen при формате "path\n```code```".
    """
    lines = code.split("\n")
    if not lines:
        return code

    first_line = lines[0].strip()
    # Если первая строка == путь или его basename — удаляем
    if first_line == filepath or first_line == filepath.split("/")[-1]:
        return "\n".join(lines[1:]).lstrip("\n")

    # Если первая строка содержит ".py"/".js"/etc и НЕ содержит операторов — вероятно мусор
    if any(
        first_line.endswith(ext) for ext in (".py", ".js", ".ts", ".php", ".rb", ".go")
    ):
        if not any(
            op in first_line
            for op in ("=", "(", ":", " ", "import", "from", "def", "class")
        ):
            return "\n".join(lines[1:]).lstrip("\n")

    return code


def _make_git_diff(workdir: Path, files: dict[str, str]) -> str:
    """
    Создаёт unified diff используя git worktree.
    Заменяет старую версию с /tmp.
    """
    # Применяем файлы в worktree
    apply_diff_to_worktree(workdir, files)

    # Получаем diff от main
    return get_diff_from_main(workdir)


async def execute(
    task: TaskInput, workdir: Optional[Path] = None, timeout_sec: int = 300
) -> CodeArtifact:
    """
    Главная функция слоя EXECUTION с git worktrees.
    """
    artifact_id = str(uuid.uuid4())[:8]
    logs = []

    def log(msg: str):
        logs.append(msg)
        console.print(f"[cyan][{artifact_id}][/cyan] {msg}")

    # 1. Создаём git worktree вместо tempdir
    log(f"creating git worktree for {task['task_id']}")
    worktree_path = create_worktree(task["task_id"], artifact_id)
    log(f"worktree: {worktree_path}")

    # 2. Чтение контекстных файлов (теперь из worktree)
    log(f"reading {len(task['context_files'])} context files")
    context_contents = {}
    for path in task["context_files"]:
        p = worktree_path / path
        if p.exists():
            context_contents[path] = p.read_text()

    # 3. Формирование промпта
    user_prompt = _build_user_prompt(task, context_contents)
    log(f"prompt length: {len(user_prompt)} chars")

    # 4. Вызов Ollama
    log("calling Ollama (qwen2.5-coder:3b-instruct)")
    llm = ChatOpenAI(
        base_url=os.getenv("OPENAI_API_BASE", "http://localhost:11434/v1"),
        api_key=os.getenv("OPENAI_API_KEY", "ollama"),
        model=os.getenv("MODEL_NAME", "qwen2.5-coder:3b-instruct"),
        temperature=0.2,
        max_tokens=2048,
        request_timeout=timeout_sec,
    )

    try:
        response = await asyncio.to_thread(
            llm.invoke,
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_prompt)],
        )
        generated_text = response.content
        log(f"generated {len(generated_text)} chars")
    except Exception as e:
        log(f"❌ Ollama call failed: {e}")
        cleanup_worktree(worktree_path, delete_branch=True)
        raise

    # 5. Парсинг файлов
    files = _parse_code_blocks(generated_text, fallback_path=task["target_path"])
    log(f"parsed {len(files)} files: {list(files.keys())}")

    if not files:
        log("❌ no code blocks found in response")
        cleanup_worktree(worktree_path, delete_branch=True)
        raise ValueError("LLM response contained no valid code blocks")

    # 6. Применяем файлы в worktree и получаем diff
    git_diff = _make_git_diff(worktree_path, files)
    log(f"diff size: {len(git_diff)} chars")

    # 7. Commit изменений в worktree
    commit_message = f"feat({task['task_id']}): {task['description'][:50]}"
    committed = commit_worktree(worktree_path, commit_message)

    if not committed:
        log("⚠ No changes to commit")

    # 8. Формирование артефакта
    raw_artifact = {
        "artifact_id": artifact_id,
        "task_id": task["task_id"],
        "files_changed": list(files.keys()),
        "git_diff": git_diff,
        "logs": "\n".join(logs),
        "worktree_path": str(worktree_path),  # ← НОВОЕ: сохраняем путь
        "branch_name": f"agent/{task['task_id']}-{artifact_id}",  # ← НОВОЕ: имя branch
    }

    sealed = seal_artifact_for_verification(raw_artifact)
    log(f"✅ artifact sealed in branch {sealed.get('branch_name', 'unknown')}")

    return sealed


# ─── Изолированный тест ─────────────────────────────────────────────────────
if __name__ == "__main__":
    """
    Запуск: python -m nodes.execution
    Тестирует execution.py изолированно, БЕЗ planning.py.
    """
    from dotenv import load_dotenv

    load_dotenv()

    # Тестовая задача — имитируем вход от Planning
    test_task: TaskInput = {
        "task_id": "test-001",
        "description": (
            "Create a Python function `is_palindrome(s: str) -> bool` "
            "that checks if a string reads the same forwards and backwards. "
            "Handle case insensitivity and ignore spaces."
        ),
        "context_files": [],  # Нет контекста для изолированного теста
        "language": "python",
        "target_path": "src/utils/palindrome.py",
        # ❌ НЕТ acceptance_criteria, test_cases, rubric
    }

    console.print("\n[bold magenta]═══ EXECUTION NODE TEST ═══[/]\n")

    try:
        result = asyncio.run(execute(test_task))

        console.print("\n[bold green]═══ RESULT ═══[/]")
        console.print(f"artifact_id: {result['artifact_id']}")
        console.print(f"files: {result['files_changed']}")
        console.print(f"diff preview:\n{result['git_diff'][:500]}")

        # Проверяем, что запрещённые поля действительно отсутствуют
        forbidden = {"worker_id", "task_description", "original_prompt"}
        leaked = forbidden & set(result.keys())
        if leaked:
            console.print(f"[bold red]🚫 GOODHART VIOLATION: leaked {leaked}[/]")
        else:
            console.print("[green]✅ Goodhart-proof: no forbidden fields leaked[/]")

    except Exception as e:
        console.print(f"[red]❌ Execution failed: {e}[/]")
