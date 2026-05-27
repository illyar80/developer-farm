"""
LangGraph Node Wrappers
-----------------------
Wrappers around the existing `nodes/planning.py`, `execution.py`, and `verification.py` modules.
Adds logging, cost tracking, and abstract feedback generation.
"""

import time
from pathlib import Path
from typing import Literal, cast

from langchain_core.runnables import RunnableConfig
from rich.console import Console

from contracts import TaskInput, Verdict
from nodes.execution import execute
from nodes.planning import plan
from nodes.verification import DEFAULT_RUBRIC, verify
from utils.git_worktree import cleanup_worktree

console = Console()

# ─── Model cost estimates ───────────────────────────────────────────────────
COST_PER_CALL = {
    "planning": 0.02,
    "execution": 0.00,
    "verification": 0.01,
}


def generate_abstract_feedback(verdict: Verdict) -> str:
    """
    Convert a detailed verdict into abstract feedback.
    Does NOT reveal the rubric — only high-level guidance.
    """
    feedback_parts = []
    score = verdict["score"]
    reason = verdict["reason"].lower()

    if score < 0.5:
        feedback_parts.append("Code needs significant improvements")
    elif score < 0.7:
        feedback_parts.append("Code quality needs improvement")
    elif score < 0.85:
        feedback_parts.append("Code is acceptable but can be better")

    # High-level guidance
    if any(word in reason for word in ["doc", "document", "comment"]):
        feedback_parts.append("Consider adding documentation for public APIs")
    if any(word in reason for word in ["error", "edge case", "exception"]):
        feedback_parts.append("Review error handling and edge cases")
    if any(word in reason for word in ["type", "annotation", "hint"]):
        feedback_parts.append("Ensure proper type annotations")
    if any(word in reason for word in ["style", "readab", "pep"]):
        feedback_parts.append("Follow language-specific style conventions")

    if not feedback_parts:
        feedback_parts.append("Review code for potential improvements")

    feedback = ". ".join(feedback_parts) + "."

    # Safety: remove specific rubric terminology
    forbidden_terms = ["docstring", "min_score", "rubric", "criterion", "score"]
    for term in forbidden_terms:
        if term.lower() in feedback.lower():
            feedback = feedback.replace(term, "quality aspect")

    return feedback


async def planning_node(state: dict, config: RunnableConfig) -> dict:
    """Planning node: user-spec → TaskInput."""
    console.print("\n[bold cyan]🧠 LANGGRAPH: Planning Node[/]")

    user_spec_path = Path(state["user_spec_path"])

    start = time.time()
    task = await plan(user_spec_path, state.get("feature_name"))
    duration = time.time() - start

    console.print(f"⏱  {duration:.1f}s | 💰 ${COST_PER_CALL['planning']:.3f}")

    return {
        "task": task,
        "iteration": 0,  # Initialize the retry counter
        "total_cost": COST_PER_CALL["planning"],
    }


async def execution_node(state: dict, config: RunnableConfig) -> dict:
    """Execution node: TaskInput → CodeArtifact."""
    console.print(
        f"\n[bold cyan]⚙️  LANGGRAPH: Execution Node (iter {state['iteration'] + 1})[/]"
    )

    task = cast(TaskInput, state["task"])
    feedback = state.get("feedback", "")

    # Add feedback to the task description when available
    if feedback:
        task_with_feedback = dict(task)
        task_with_feedback["description"] = (
            f"{task['description']}\n\n"
            f"## Previous Attempt Feedback\n"
            f"{feedback}\n\n"
            f"Please address these issues in the implementation."
        )
        task = cast(TaskInput, task_with_feedback)
        console.print(f"💬 Feedback: {feedback[:80]}...")

    start = time.time()
    artifact = await execute(task)
    duration = time.time() - start

    console.print(f"⏱  {duration:.1f}s | 💰 ${COST_PER_CALL['execution']:.3f}")

    # Store the artifact in an append-only list
    return {
        "artifacts": [artifact],
        "iteration": 1,  # Increment (Annotated with operator.add)
        "total_cost": COST_PER_CALL["execution"],
    }


async def verification_node(state: dict, config: RunnableConfig) -> dict:
    """Verification node: CodeArtifact → Verdict."""
    console.print("\n[bold magenta]🔍 LANGGRAPH: Verification Node[/]")

    # Take the latest artifact
    artifact = state["artifacts"][-1]

    start = time.time()
    verdict = await verify(artifact, DEFAULT_RUBRIC)
    duration = time.time() - start

    console.print(f"⏱  {duration:.1f}s | 💰 ${COST_PER_CALL['verification']:.3f}")
    console.print(
        f"📊 Score: {verdict['score']:.2f} | {'✅ PASS' if verdict['passed'] else '❌ FAIL'}"
    )

    # Clean up the worktree on FAIL to avoid cluttering the repository
    if not verdict["passed"] and "worktree_path" in artifact:
        worktree_path = Path(artifact["worktree_path"])
        if worktree_path.exists():
            console.print(f"[yellow]🧹 Cleaning up failed worktree: {worktree_path}[/]")
            cleanup_worktree(worktree_path, delete_branch=True)

    # Generate abstract feedback on FAIL
    feedback = ""
    if not verdict["passed"]:
        feedback = generate_abstract_feedback(verdict)
        console.print(f"💬 Abstract feedback: {feedback[:100]}...")

    return {
        "verdicts": [verdict],
        "feedback": feedback,
        "total_cost": COST_PER_CALL["verification"],
    }


def should_retry(state: dict) -> Literal["retry", "done"]:
    """
    Conditional edge that decides whether to retry or finish.
    Returns `"retry"` on FAIL while `iteration < 3`, otherwise `"done"`.
    """
    if not state.get("verdicts"):
        return "retry"  # First iteration

    last_verdict = state["verdicts"][-1]
    iteration = state.get("iteration", 0)

    if last_verdict["passed"]:
        console.print(f"\n[bold green]✅ PASS on iteration {iteration}. Stopping.[/]")
        return "done"

    if iteration >= 3:
        console.print("\n[bold red]💀 Max retries (3) reached. Stopping.[/]")
        return "done"

    console.print(f"\n[bold yellow]🔄 FAIL on iteration {iteration}. Retrying...[/]")
    return "retry"
