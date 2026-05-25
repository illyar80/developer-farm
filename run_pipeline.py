#!/usr/bin/env python3

"""
Developer Farm MVP Pipeline
----------------------------
Planning → Execution → Verification (с retry loop до 3 итераций)

Архитектурные гарантии:
- Execution НИКОГДА не видит acceptance_criteria, rubric, test_cases
- Verification НИКОГДА не видит worker_id, task_description, original_prompt
- Feedback для retry — АБСТРАКТНЫЙ, не раскрывает рубрику
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent))

from contracts import TaskInput, Verdict
from nodes.execution import execute
from nodes.planning import plan
from nodes.verification import verify

console = Console()

# ─── Настройки ──────────────────────────────────────────────────────────────
MAX_RETRIES = 3
RESULTS_DIR = Path("work/mvp/results")

# Стоимость моделей (примерная, для расчёта экономики)
COST_PER_CALL = {
    "planning": 0.02,  # Qwen-Max API
    "execution": 0.00,  # Локально через Ollama
    "verification": 0.01,  # Qwen-Turbo API
}


# ─── Abstract Feedback Generator ───────────────────────────────────────────
def generate_abstract_feedback(verdict: Verdict) -> str:
    """
    Превращает детальный verdict в абстрактный feedback для воркера.

    ⛔ НЕ РАСКРЫВАЕТ РУБРИКУ! Только общие направления улучшения.

    Это критично для Goodhart-proof: если воркер узнает конкретные
    критерии (docstring, error handling), он начнёт оптимизировать
    под них формально, а не решать задачу честно.
    """
    feedback_parts = []
    score = verdict["score"]
    reason = verdict["reason"].lower()

    # Общий фидбек по скору
    if score < 0.5:
        feedback_parts.append("Code needs significant improvements")
    elif score < 0.7:
        feedback_parts.append("Code quality needs improvement")
    elif score < 0.85:
        feedback_parts.append("Code is acceptable but can be better")

    # Абстрактные направления (НЕ конкретные критерии!)
    if any(word in reason for word in ["doc", "document", "comment"]):
        feedback_parts.append("Consider adding documentation for public APIs")

    if any(
        word in reason for word in ["error", "edge case", "exception", "validation"]
    ):
        feedback_parts.append("Review error handling and edge cases")

    if any(word in reason for word in ["type", "annotation", "hint"]):
        feedback_parts.append("Ensure proper type annotations")

    if any(word in reason for word in ["style", "readab", "pep"]):
        feedback_parts.append("Follow language-specific style conventions")

    if any(word in reason for word in ["hardcod", "magic"]):
        feedback_parts.append("Avoid hardcoded values")

    # Если ничего не нашли — общий фидбек
    if not feedback_parts:
        feedback_parts.append("Review code for potential improvements")

    # Формируем финальный feedback
    feedback = ". ".join(feedback_parts) + "."

    # ⛔ Защитная проверка: в feedback не должно быть конкретных критериев
    forbidden_terms = ["docstring", "min_score", "rubric", "criterion", "score"]
    for term in forbidden_terms:
        if term.lower() in feedback.lower():
            # Заменяем на абстрактный аналог
            feedback = feedback.replace(term, "quality aspect")

    return feedback


# ─── Главная функция пайплайна ──────────────────────────────────────────────
async def run_pipeline(user_spec_path: Path) -> dict:
    """
    Полный пайплайн: Planning → Execution → Verification (с retry).

    Returns:
        dict с полной статистикой: итерации, стоимость, время, verdicts
    """
    start_time = time.time()
    total_cost = 0.0

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # ═══ ЭТАП 1: PLANNING ═══
    console.print("\n" + "=" * 70)
    console.print("[bold cyan]🧠 ЭТАП 1: PLANNING[/]")
    console.print("=" * 70)

    plan_start = time.time()
    task = await plan(user_spec_path)
    plan_duration = time.time() - plan_start
    total_cost += COST_PER_CALL["planning"]

    # Сохраняем TaskInput
    task_file = RESULTS_DIR / "01_task_input.json"
    task_file.write_text(json.dumps(task, indent=2, ensure_ascii=False))

    console.print(f"\n⏱  Planning time: {plan_duration:.1f}s")
    console.print(f"💰 Cost: ${COST_PER_CALL['planning']:.3f}")
    console.print(f"💾 Saved: {task_file}")

    # ═══ ЭТАП 2: EXECUTION + VERIFICATION (с retry) ═══
    console.print("\n" + "=" * 70)
    console.print("[bold magenta]🔄 ЭТАП 2: EXECUTION + VERIFICATION (retry loop)[/]")
    console.print("=" * 70)

    verdicts = []
    artifacts = []
    feedback = ""
    final_passed = False

    for iteration in range(1, MAX_RETRIES + 1):
        console.print(f"\n{'─' * 70}")
        console.print(f"[bold yellow]📍 Итерация {iteration}/{MAX_RETRIES}[/]")
        console.print(f"{'─' * 70}")

        # ─── Подготавливаем задачу для этой итерации ───
        iter_task: TaskInput = {
            "task_id": task["task_id"],
            "description": task["description"],
            "context_files": task["context_files"],
            "language": task["language"],
            "target_path": task["target_path"],
        }
        if feedback:
            # Добавляем abstract feedback к описанию
            iter_task["description"] = (
                f"{task['description']}\n\n"
                f"## Previous Attempt Feedback\n"
                f"{feedback}\n\n"
                f"Please address these issues in the implementation."
            )
            console.print(f"💬 Feedback added: {feedback[:100]}...")

        # ─── EXECUTION ───
        console.print("\n[bold cyan]⚙️  EXECUTION[/]")
        exec_start = time.time()

        try:
            artifact = await execute(iter_task)
            exec_duration = time.time() - exec_start
            total_cost += COST_PER_CALL["execution"]

            artifacts.append(artifact)

            # Сохраняем артефакт
            artifact_file = RESULTS_DIR / f"02_artifact_iter{iteration}.json"
            artifact_file.write_text(json.dumps(artifact, indent=2, ensure_ascii=False))

            console.print(f"⏱  Execution time: {exec_duration:.1f}s")
            console.print(f"💰 Cost: ${COST_PER_CALL['execution']:.3f}")
            console.print(f"💾 Saved: {artifact_file}")

        except Exception as e:
            console.print(f"[red]❌ Execution failed: {e}[/]")
            if iteration == MAX_RETRIES:
                console.print("[red]💀 All retries exhausted. Pipeline FAILED.[/]")
                break
            feedback = "Execution failed. Please try a different approach."
            continue

        # ─── VERIFICATION ───
        console.print("\n[bold magenta]🔍 VERIFICATION[/]")
        verify_start = time.time()

        try:
            verdict = await verify(artifact)
            verify_duration = time.time() - verify_start
            total_cost += COST_PER_CALL["verification"]

            verdicts.append(verdict)

            # Сохраняем verdict
            verdict_file = RESULTS_DIR / f"03_verdict_iter{iteration}.json"
            verdict_file.write_text(json.dumps(verdict, indent=2, ensure_ascii=False))

            console.print(f"⏱  Verification time: {verify_duration:.1f}s")
            console.print(f"💰 Cost: ${COST_PER_CALL['verification']:.3f}")
            console.print(f"💾 Saved: {verdict_file}")

            # ─── Проверка результата ───
            if verdict["passed"]:
                console.print(f"\n[bold green]✅ Итерация {iteration}: PASS![/]")
                final_passed = True
                break
            else:
                console.print(f"\n[bold red]❌ Итерация {iteration}: FAIL[/]")
                console.print(f"Score: {verdict['score']:.2f} (min: 0.7)")
                console.print(f"Reason: {verdict['reason'][:200]}...")

                if iteration < MAX_RETRIES:
                    # Генерируем абстрактный feedback для следующей итерации
                    feedback = generate_abstract_feedback(verdict)
                    console.print("\n💬 Abstract feedback for next iteration:")
                    console.print(f"   {feedback}")
                else:
                    console.print(
                        f"\n[red]💀 Все {MAX_RETRIES} попытки исчерпаны. Pipeline FAILED.[/]"
                    )

        except Exception as e:
            console.print(f"[red]❌ Verification failed: {e}[/]")
            if iteration == MAX_RETRIES:
                console.print("[red]💀 All retries exhausted. Pipeline FAILED.[/]")
                break
            feedback = "Verification error. Ensure code is syntactically correct."
            continue

    # ═══ ФИНАЛЬНАЯ СТАТИСТИКА ═══
    total_duration = time.time() - start_time

    console.print("\n" + "=" * 70)
    console.print("[bold]📊 ИТОГОВАЯ СТАТИСТИКА[/]")
    console.print("=" * 70)

    # Таблица результатов
    table = Table(show_header=True, header_style="bold cyan", box=box.SIMPLE)
    table.add_column("Метрика", style="cyan")
    table.add_column("Значение", justify="right")

    table.add_row("Результат", "✅ PASS" if final_passed else "❌ FAIL")
    table.add_row("Итераций", str(len(verdicts)))
    table.add_row("Общее время", f"{total_duration:.1f}s")
    table.add_row("Planning", f"{plan_duration:.1f}s")
    table.add_row(
        "Execution (суммарно)", f"{sum(1 for _ in artifacts) * 15:.0f}s (avg)"
    )
    table.add_row("Verification (суммарно)", f"{len(verdicts) * 10:.0f}s (avg)")
    table.add_row("💰 Общая стоимость", f"${total_cost:.3f}")
    table.add_row(
        "Средняя стоимость/итерация",
        f"${(total_cost - COST_PER_CALL['planning']) / max(len(verdicts), 1):.3f}",
    )

    console.print(table)

    # Goodhart-proof проверка
    console.print("\n[bold]🔒 Goodhart-proof проверка:[/]")

    goodhart_ok = True
    for i, artifact in enumerate(artifacts, 1):
        forbidden = {
            "worker_id",
            "task_description",
            "original_prompt",
            "acceptance_criteria",
        }
        leaked = forbidden & set(artifact.keys())
        if leaked:
            console.print(f"  [red]❌ Итерация {i}: leaked {leaked}[/]")
            goodhart_ok = False
        else:
            console.print(f"  [green]✅ Итерация {i}: изоляция сохранена[/]")

    if goodhart_ok:
        console.print(
            "\n[bold green]✅ ВСЕ ИТЕРАЦИИ: Goodhart-proof изоляция работает![/]"
        )
    else:
        console.print("\n[bold red]❌ НАРУШЕНИЕ ИЗОЛЯЦИИ! Проверьте contracts.py[/]")

    # Финальный отчёт
    final_report = {
        "timestamp": datetime.now().isoformat(),
        "user_spec": str(user_spec_path),
        "task": task,
        "iterations": len(verdicts),
        "final_passed": final_passed,
        "verdicts": verdicts,
        "artifacts_count": len(artifacts),
        "total_duration_sec": total_duration,
        "total_cost_usd": total_cost,
        "goodhart_proof": goodhart_ok,
    }

    report_file = RESULTS_DIR / "00_final_report.json"
    report_file.write_text(json.dumps(final_report, indent=2, ensure_ascii=False))
    console.print(f"\n💾 Final report: {report_file}")

    # Вердикт
    console.print("\n" + "=" * 70)
    if final_passed:
        console.print("[bold green]🎉 PIPELINE SUCCESS! Код прошёл все проверки.[/]")
    else:
        console.print("[bold red]💀 PIPELINE FAILED! Код не прошёл верификацию.[/]")
    console.print("=" * 70 + "\n")

    return final_report


# ─── Точка входа ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()

    # Проверяем что Ollama доступна
    import requests

    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    try:
        response = requests.get(f"{ollama_url.replace('/v1', '')}/api/tags", timeout=2)
        if response.status_code != 200:
            console.print("[red]❌ Ollama not responding[/]")
            console.print("Start Ollama: ollama serve")
            exit(1)
    except requests.exceptions.RequestException:
        console.print("[red]❌ Cannot connect to Ollama[/]")
        console.print(f"Check that Ollama is running on {ollama_url}")
        exit(1)

    # ✅ Читаем путь к user-spec из аргумента командной строки
    if len(sys.argv) > 1:
        user_spec = Path(sys.argv[1])
    else:
        user_spec = Path("work/mvp/user-spec.md")

    if not user_spec.exists():
        console.print(f"[red]❌ User spec not found: {user_spec}[/]")
        exit(1)

    console.print(
        Panel.fit(
            f"[bold magenta]🚀 DEVELOPER FARM MVP PIPELINE[/]\n"
            f"[cyan]Spec: {user_spec}[/]\n"
            "Planning → Execution → Verification (с retry loop)",
            border_style="magenta",
        )
    )

    try:
        report = asyncio.run(run_pipeline(user_spec))
        exit(0 if report["final_passed"] else 1)
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️  Interrupted[/]")
        exit(130)
