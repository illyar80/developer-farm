"""
Goodhart-Proof Contracts for Developer Farm MVP
-----------------------------------------------
Эти TypedDict определяют ЖЁСТКИЕ границы между слоями.
Если поля нет в TypedDict — оно физически не может быть передано.

Принцип: важно не то, что слой делает, а чего он НЕ видит.
"""

from typing import Literal, TypedDict

# ═══════════════════════════════════════════════════════════════════
# СЛОЙ PLANNING → EXECUTION
# ═══════════════════════════════════════════════════════════════════


class TaskInput(TypedDict):
    """
    Что получает воркер от Planning.
    ⛔ ЗАПРЕЩЕНО добавлять: acceptance_criteria, tests, rubric, worker_id
    """

    task_id: str  # "task-001"
    description: str  # Что нужно сделать (технически)
    context_files: list[str]  # Пути к файлам для чтения
    language: str  # "python", "php", "typescript"
    target_path: str  # Куда писать результат
    # ❌ acceptance_criteria: str      ← НЕЛЬЗЯ
    # ❌ test_cases: list[dict]        ← НЕЛЬЗЯ
    # ❌ rubric: dict                  ← НЕЛЬЗЯ


# ═══════════════════════════════════════════════════════════════════
# СЛОЙ EXECUTION → VERIFICATION
# ═══════════════════════════════════════════════════════════════════


class CodeArtifact(TypedDict):
    """
    Запечатанный артефакт от воркера.
    ⛔ ЗАПРЕЩЕНО добавлять: worker_id, task_description, original_prompt
    """

    artifact_id: str  # UUID
    task_id: str  # Ссылка на задачу (для трассировки)
    files_changed: list[str]  # Список изменённых файлов
    git_diff: str  # Патч в формате unified diff
    logs: str  # Логи процесса генерации
    worktree_path: NotRequired[str]  # ← НОВОЕ
    branch_name: NotRequired[str]  # ← НОВОЕ
    # ❌ worker_id: str                ← НЕЛЬЗЯ (верификатор не должен знать автора)
    # ❌ task_description: str         ← НЕЛЬЗЯ (оценка только по коду)
    # ❌ prompt_sent: str              ← НЕЛЬЗЯ


# ═══════════════════════════════════════════════════════════════════
# СЛОЙ VERIFICATION → OPTIMIZATION / ЧЕЛОВЕК
# ═══════════════════════════════════════════════════════════════════


class Verdict(TypedDict):
    """
    Результат слепой верификации.
    """

    artifact_id: str
    task_id: str
    passed: bool
    score: float  # 0.0 - 1.0
    reason: str  # Почему pass/fail
    rubric_applied: dict  # По каким критериям оценивали
    tests_passed: int
    tests_total: int
    # ❌ worker_id: str                ← НЕЛЬЗЯ
    # ❌ task_description: str         ← НЕЛЬЗЯ


# ═══════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ
# ═══════════════════════════════════════════════════════════════════


class UserSpec(TypedDict):
    """Вход для Planning (что пишет человек)"""

    feature_name: str
    description: str
    goals: list[str]
    constraints: list[str]


class VerificationRubric(TypedDict):
    """Рубрика для Verifier (НЕ видна воркеру!)"""

    criteria: list[str]
    min_score: float
    required_tests: list[str]


# ═══════════════════════════════════════════════════════════════════
# ВАЛИДАТОР ГРАНИЦ (вызывается при передаче между слоями)
# ═══════════════════════════════════════════════════════════════════

FORBIDDEN_IN_TASK_INPUT = {"acceptance_criteria", "test_cases", "rubric", "worker_id"}
FORBIDDEN_IN_ARTIFACT = {
    "worker_id",
    "task_description",
    "original_prompt",
    "prompt_sent",
}


def validate_boundary(data: dict, allowed_type: type, forbidden: set) -> dict:
    """
    Проверяет, что в данных нет запрещённых полей.
    Вызывается на каждом переходе между слоями.
    """
    extra_keys = set(data.keys()) - set(allowed_type.__annotations__.keys())
    forbidden_present = extra_keys & forbidden

    if forbidden_present:
        raise ValueError(
            f"🚫 GOODHART VIOLATION: forbidden fields detected: {forbidden_present}. "
            f"This breaks layer isolation!"
        )

    # Возвращаем ТОЛЬКО разрешённые поля (остальное отбрасываем)
    return {k: v for k, v in data.items() if k in allowed_type.__annotations__}


# ═══════════════════════════════════════════════════════════════════
# УТИЛИТЫ ДЛЯ СЛОЁВ
# ═══════════════════════════════════════════════════════════════════


def seal_task_for_execution(task: dict) -> TaskInput:
    """Planning → Execution: обрезаем всё лишнее"""
    return validate_boundary(task, TaskInput, FORBIDDEN_IN_TASK_INPUT)


def seal_artifact_for_verification(artifact: dict) -> CodeArtifact:
    """Execution → Verification: скрываем автора и контекст"""
    return validate_boundary(artifact, CodeArtifact, FORBIDDEN_IN_ARTIFACT)
