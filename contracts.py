"""
Goodhart-Proof Contracts for Developer Farm MVP
-----------------------------------------------
These `TypedDict` definitions enforce HARD boundaries between layers.
If a field is not declared in a `TypedDict`, it cannot be passed across the boundary.

Principle: what matters is not only what a layer does, but also what it must NOT see.
"""

from typing import TypedDict, TypeVar, cast

from typing_extensions import NotRequired

# ═══════════════════════════════════════════════════════════════════
# LAYER PLANNING → EXECUTION
# ═══════════════════════════════════════════════════════════════════


class TaskInput(TypedDict):
    """
    What the worker receives from Planning.
    ⛔ FORBIDDEN to add: `acceptance_criteria`, `tests`, `rubric`, `worker_id`
    """

    task_id: str  # "task-001"
    description: str  # What must be implemented, technically
    context_files: list[str]  # File paths to read for context
    language: str  # "python", "php", "typescript"
    target_path: str  # Where to write the result
    # ❌ acceptance_criteria: str      ← FORBIDDEN
    # ❌ test_cases: list[dict]        ← FORBIDDEN
    # ❌ rubric: dict                  ← FORBIDDEN


# ═══════════════════════════════════════════════════════════════════
# LAYER EXECUTION → VERIFICATION
# ═══════════════════════════════════════════════════════════════════


class CodeArtifact(TypedDict):
    """
    Sealed artifact produced by the worker.
    ⛔ FORBIDDEN to add: `worker_id`, `task_description`, `original_prompt`
    """

    artifact_id: str  # UUID
    task_id: str  # Reference to the task, for traceability
    files_changed: list[str]  # List of modified files
    git_diff: str  # Patch in unified diff format
    logs: str  # Generation process logs
    worktree_path: NotRequired[str]  # New
    branch_name: NotRequired[str]  # New
    # ❌ worker_id: str                ← FORBIDDEN (the verifier must not know the author)
    # ❌ task_description: str         ← FORBIDDEN (evaluation must be code-only)
    # ❌ prompt_sent: str              ← FORBIDDEN


# ═══════════════════════════════════════════════════════════════════
# LAYER VERIFICATION → OPTIMIZATION / HUMAN
# ═══════════════════════════════════════════════════════════════════


class Verdict(TypedDict):
    """
    Result of blind verification.
    """

    artifact_id: str
    task_id: str
    passed: bool
    score: float  # 0.0 - 1.0
    reason: str  # Why it passed or failed
    rubric_applied: dict  # Which evaluation criteria were applied
    tests_passed: int
    tests_total: int
    # ❌ worker_id: str                ← FORBIDDEN
    # ❌ task_description: str         ← FORBIDDEN


# ═══════════════════════════════════════════════════════════════════
# SUPPORTING TYPES
# ═══════════════════════════════════════════════════════════════════


class UserSpec(TypedDict):
    """Input for Planning, written by a human."""

    feature_name: str
    description: str
    goals: list[str]
    constraints: list[str]


class VerificationRubric(TypedDict):
    """Rubric for the verifier, not visible to the worker."""

    criteria: list[str]
    min_score: float
    required_tests: list[str]


# ═══════════════════════════════════════════════════════════════════
# BOUNDARY VALIDATOR (called on every layer transition)
# ═══════════════════════════════════════════════════════════════════

FORBIDDEN_IN_TASK_INPUT = {"acceptance_criteria", "test_cases", "rubric", "worker_id"}
FORBIDDEN_IN_ARTIFACT = {
    "worker_id",
    "task_description",
    "original_prompt",
    "prompt_sent",
}


T = TypeVar("T")


def validate_boundary(data: dict, allowed_type: type[T], forbidden: set) -> T:
    """
    Verify that the data does not contain forbidden fields.
    Called on every transition between layers.
    """
    extra_keys = set(data.keys()) - set(allowed_type.__annotations__.keys())
    forbidden_present = extra_keys & forbidden

    if forbidden_present:
        raise ValueError(
            f"🚫 GOODHART VIOLATION: forbidden fields detected: {forbidden_present}. "
            f"This breaks layer isolation!"
        )

    # Return ONLY allowed fields and discard everything else
    filtered = {k: v for k, v in data.items() if k in allowed_type.__annotations__}
    return cast(T, filtered)


# ═══════════════════════════════════════════════════════════════════
# LAYER UTILITIES
# ═══════════════════════════════════════════════════════════════════


def seal_task_for_execution(task: dict) -> TaskInput:
    """Planning → Execution: strip everything except allowed fields."""
    return validate_boundary(task, TaskInput, FORBIDDEN_IN_TASK_INPUT)


def seal_artifact_for_verification(artifact: dict) -> CodeArtifact:
    """Execution → Verification: hide author-related and extra context fields."""
    return validate_boundary(artifact, CodeArtifact, FORBIDDEN_IN_ARTIFACT)
