"""
VERIFICATION LAYER — Blind Code Reviewer
----------------------------------------
Получает ТОЛЬКО SealedArtifact (git diff + logs) + рубрику.
НЕ видит: worker_id, task_description, original_prompt.

Возвращает Verdict: pass/fail + score + reason.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))
from contracts import CodeArtifact, Verdict, VerificationRubric

console = Console()

# Используем Ollama локально
DEFAULT_OLLAMA_MODEL = os.getenv(
    "OLLAMA_VERIFIER_MODEL", os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b-instruct")
)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")


# ─── Рубрика (жёстко задана, НЕ передаётся воркеру!) ────────────────────────
DEFAULT_RUBRIC: VerificationRubric = {
    "criteria": [
        "Code correctness: implements the described functionality without bugs",
        "Type hints: all functions have proper type annotations",
        "Error handling: edge cases handled gracefully (e.g., division by zero)",
        "Code style: PEP 8 compliant, readable, idiomatic Python",
        "No hardcoded values or magic numbers",
        "Docstrings for public functions",
    ],
    "min_score": 0.7,
    "required_tests": [],
}

# ─── Системный промпт для верификатора ──────────────────────────────────────
SYSTEM_PROMPT = """You are an independent code reviewer. You receive:
1. A git diff showing code changes (new files or modifications)
2. A rubric with specific criteria to evaluate

Your job:
- Review the code ONLY based on the diff and rubric
- Do NOT assume context you don't have
- Be objective and strict
- Score each criterion from 0.0 to 1.0
- Provide a final score (average of all criteria)
- Give a clear pass/fail verdict with reasoning

Output format (JSON):
```json
{
  "criteria_scores": {
    "criterion_1": 0.9,
    "criterion_2": 0.8
  },
  "final_score": 0.85,
  "passed": true,
  "reason": "Code is correct, well-typed, handles edge cases properly. Minor style issue in line 12."
}
```

Be strict. If code has bugs, missing type hints, or poor error handling — fail it."""


def _get_ollama_model() -> str:
    """Возвращает название модели Ollama."""
    return DEFAULT_OLLAMA_MODEL


def _is_no_endpoints_error(exc: Exception) -> bool:
    return "No endpoints found for" in str(exc)


def _write_reconstructed_file(
    workdir: Path, relative_path: str, lines: list[str]
) -> None:
    filepath = workdir / relative_path
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text("\n".join(lines) + ("\n" if lines else ""))


def _apply_diff_to_workdir(workdir: Path, git_diff: str) -> list[str]:
    """
    Применяет git diff к временной директории для запуска тестов.

    Для MVP поддерживает реконструкцию файлов из unified diff, когда в патче
    присутствует достаточно строк для сборки содержимого (например diff against
    /dev/null, как в execution.py).
    """
    files: list[str] = []
    current_file: str | None = None
    current_lines: list[str] = []
    in_hunk = False

    def flush_current_file() -> None:
        nonlocal current_file, current_lines
        if current_file is None:
            return
        _write_reconstructed_file(workdir, current_file, current_lines)
        files.append(current_file)
        current_file = None
        current_lines = []

    for line in git_diff.splitlines():
        if line.startswith("+++ "):
            flush_current_file()
            path_part = line[4:].strip()
            if path_part == "/dev/null":
                current_file = None
                current_lines = []
            else:
                current_file = (
                    path_part[2:] if path_part.startswith("b/") else path_part
                )
                current_lines = []
            in_hunk = False
            continue

        if current_file is None:
            continue

        if line.startswith("@@ "):
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if line.startswith("+") and not line.startswith("+++"):
            current_lines.append(line[1:])
        elif line.startswith(" "):
            current_lines.append(line[1:])
        elif line.startswith("\\ No newline at end of file"):
            continue
        elif line.startswith("-") and not line.startswith("---"):
            continue

    flush_current_file()
    return files


def _run_tests(workdir: Path, files: list[str]) -> tuple[bool, int, int]:
    """
    Запускает pytest в workdir если есть тесты.
    Возвращает (passed, tests_passed, tests_total).
    """
    test_files = [
        f for f in files if f.startswith("tests/") or "test" in Path(f).name.lower()
    ]

    if not test_files:
        console.print("  [yellow]⚠ No test files found in artifact[/]")
        return True, 0, 0

    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "-v", "--tb=short", *test_files],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=30,
        )

        passed = result.returncode == 0
        output = result.stdout + result.stderr

        passed_match = re.search(r"(\d+) passed", output)
        failed_match = re.search(r"(\d+) failed", output)

        tests_passed = int(passed_match.group(1)) if passed_match else 0
        tests_failed = int(failed_match.group(1)) if failed_match else 0
        tests_total = tests_passed + tests_failed

        console.print(
            f"  {'✅' if passed else '❌'} Tests: {tests_passed}/{tests_total} passed"
        )
        return passed, tests_passed, tests_total
    except subprocess.TimeoutExpired:
        console.print("  [red]❌ Tests timed out (>30s)[/]")
        return False, 0, 0
    except Exception as e:
        console.print(f"  [red]❌ Test execution failed: {e}[/]")
        return False, 0, 0


async def verify(
    artifact: CodeArtifact,
    rubric: VerificationRubric = DEFAULT_RUBRIC,
    timeout_sec: int = 120,
) -> Verdict:
    """
    Главная функция слоя VERIFICATION.

    Args:
        artifact: CodeArtifact (ТОЛЬКО diff + logs, без worker_id!)
        rubric: Критерии оценки (НЕ видны воркеру)
        timeout_sec: Максимальное время на верификацию

    Returns:
        Verdict (pass/fail + score + reason)
    """
    console.print(
        f"\n[bold magenta]═══ VERIFICATION: {artifact['artifact_id']} ═══[/]\n"
    )

    forbidden = {"worker_id", "task_description", "original_prompt", "prompt_sent"}
    leaked = forbidden & set(artifact.keys())
    if leaked:
        console.print(f"[red]🚫 GOODHART VIOLATION: artifact contains {leaked}[/]")
        raise ValueError(f"Artifact not properly sealed: {leaked}")

    console.print("✅ Artifact sealed (no forbidden fields)")
    console.print(f"📄 Files: {artifact['files_changed']}")
    console.print(f"📝 Diff size: {len(artifact['git_diff'])} chars\n")

    workdir = Path(tempfile.mkdtemp(prefix=f"verify-{artifact['artifact_id']}-"))
    console.print(f"📁 Workdir: {workdir}")

    files = _apply_diff_to_workdir(workdir, artifact["git_diff"])
    console.print(f"✅ Applied diff: {len(files)} files\n")

    console.print("🧪 Running tests...")
    tests_passed_flag, tests_passed_count, tests_total = _run_tests(workdir, files)
    console.print()

    model_name = _get_ollama_model()
    console.print("🤖 Calling Ollama for code review...")
    console.print(f"🤖 Model: {model_name}")
    user_prompt = f"""# Git Diff
{artifact["git_diff"]}

# Rubric
{chr(10).join(f"- {criterion}" for criterion in rubric["criteria"])}

Review this code strictly according to the rubric. Output JSON as specified."""

    llm = ChatOpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",  # Ollama не требует реальный API key
        model=model_name,
        temperature=0.2,
        max_completion_tokens=1024,
        timeout=timeout_sec,
    )

    try:
        console.print(f"→ Using model: [bold]{model_name}[/]")
        response = await asyncio.to_thread(
            llm.invoke,
            [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ],
        )
        raw_output = str(response.content).strip()
        console.print(f"✅ Response from {model_name}: {len(raw_output)} chars\n")
    except Exception as e:
        console.print(f"[red]❌ Ollama call failed: {e}[/]")
        raise

    try:
        if raw_output.startswith("```json"):
            raw_output = raw_output[7:]
        if raw_output.startswith("```"):
            raw_output = raw_output[3:]
        if raw_output.endswith("```"):
            raw_output = raw_output[:-3]

        llm_verdict = json.loads(raw_output.strip())
        llm_score = float(llm_verdict.get("final_score", 0.0))
        llm_passed = bool(llm_verdict.get("passed", False))
        llm_reason = str(llm_verdict.get("reason", "No reason provided"))

        console.print(f"📊 LLM Score: {llm_score:.2f}")
        console.print(f"📝 Reason: {llm_reason[:200]}...\n")
    except json.JSONDecodeError as e:
        console.print(f"[red]❌ JSON parse error: {e}[/]")
        console.print(f"Raw output:\n{raw_output[:500]}")
        llm_score = 0.0
        llm_passed = False
        llm_reason = "Failed to parse LLM response"

    final_passed = tests_passed_flag and llm_passed and llm_score >= rubric["min_score"]

    console.print(
        f"[bold {'green' if final_passed else 'red'}]═══ FINAL VERDICT ═══[/]"
    )
    console.print(f"{'✅ PASS' if final_passed else '❌ FAIL'}")
    console.print(
        f"Tests: {'✅' if tests_passed_flag else '❌'} ({tests_passed_count}/{tests_total})"
    )
    console.print(f"LLM Score: {llm_score:.2f} (min: {rubric['min_score']})")
    console.print(f"Reason: {llm_reason}\n")

    verdict: Verdict = {
        "artifact_id": artifact["artifact_id"],
        "task_id": artifact["task_id"],
        "passed": final_passed,
        "score": llm_score,
        "reason": llm_reason,
        "rubric_applied": dict(rubric),
        "tests_passed": tests_passed_count,
        "tests_total": tests_total,
    }

    return verdict


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

    artifact_path = Path("work/mvp/results/artifact.json")

    if not artifact_path.exists():
        console.print("[yellow]⚠ Artifact not found. Run execution.py first:[/]")
        console.print("  python -m nodes.execution")
        console.print("\n[yellow]Or creating test artifact...[/]\n")

        test_artifact: CodeArtifact = {
            "artifact_id": "test-verify-001",
            "task_id": "test-001",
            "files_changed": ["src/utils/palindrome.py"],
            "git_diff": """--- /dev/null
+++ src/utils/palindrome.py
@@ -0,0 +1,4 @@
+def is_palindrome(s: str) -> bool:
+    normalized_str = ''.join(char.lower() for char in s if char.isalnum())
+    return normalized_str == normalized_str[::-1]
+""",
            "logs": "Test artifact for verification",
        }

        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(test_artifact, indent=2))
        console.print(f"✅ Created test artifact: {artifact_path}\n")

    artifact: CodeArtifact = json.loads(artifact_path.read_text())

    console.print("\n[bold magenta]═══ VERIFICATION NODE TEST ═══[/]\n")

    try:
        verdict = asyncio.run(verify(artifact))

        output_path = Path("work/mvp/results/verdict.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(verdict, indent=2))
        console.print(f"💾 Saved to: {output_path}")
    except Exception as e:
        console.print(f"[red]❌ Verification failed: {e}[/]")
        import traceback

        traceback.print_exc()
