"""
LangGraph Node Wrappers
-----------------------
Wrappers around the existing `nodes/planning.py`, `execution.py`, and `verification.py` modules.
Adds logging, cost tracking, abstract feedback generation, wave orchestration,
auto-merging, and human-in-the-loop approval.
"""

import time
import uuid
from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from utils.output import console

from contracts import TaskInput, Verdict, VerificationRubric
from nodes.context_router import context_router_node as raw_context_router
from nodes.execution import execute
from nodes.planning import plan
from utils.model_router import select_model
from utils.model_router_llm import select_model_llm
from nodes.confidence_router import (
    evaluate_and_route,
    format_static_errors,
    post_execution_confidence,
    run_static_gate,
)
from nodes.self_reflection import run_self_reflection
from nodes.static_gate import static_gate_check
from nodes.verification_consensus import get_verdict as verify
from benchmark.analyzers.rubric_generator import generate_rubric_from_spec
from utils.git_worktree import (
    apply_diff_to_worktree,
    cleanup_worktree,
    commit_worktree,
    create_worktree,
    get_diff_from_main,
    merge_worktree,
)

from utils.farm_config import get_repo_path


COST_PER_CALL = {
    "planning": 0.02,
    "context_router": 0.00,
    "execution": 0.00,
    "verification": 0.01,
}


def generate_abstract_feedback(verdict: Verdict) -> str:
    """Convert a detailed verdict into abstract feedback.
    Does NOT reveal the rubric — only high-level guidance."""
    feedback_parts = []
    score = verdict["score"]
    reason = verdict["reason"].lower()
    min_score = verdict.get("rubric_applied", {}).get("min_score", 0.6)
    if score < min_score - 0.1:
        feedback_parts.append("Code needs significant improvements")
    elif score < min_score:
        feedback_parts.append("Code quality needs improvement")
    elif score < min_score + 0.15:
        feedback_parts.append("Code is acceptable but can be improved")
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
    forbidden_terms = ["docstring", "min_score", "rubric", "criterion", "score"]
    for term in forbidden_terms:
        if term.lower() in feedback.lower():
            feedback = feedback.replace(term, "quality aspect")
    return feedback


async def planning_node(state: dict, config: RunnableConfig) -> dict:
    """Planning node: user-spec -> TaskInput + VerificationRubric (rubric is NEVER passed to execution)."""
    console.print("\n[bold cyan]LANGGRAPH: Planning Node[/]")
    user_spec_path = Path(state["user_spec_path"])
    start = time.time()
    task = await plan(user_spec_path, state.get("feature_name"))
    duration = time.time() - start
    console.print(f"Time: {duration:.1f}s | Cost: ${COST_PER_CALL['planning']:.3f}")

    # Generate task-specific rubric from the spec (Goodhart-proof: stored separately from task)
    spec_text = user_spec_path.read_text(encoding="utf-8", errors="replace")
    rubric = generate_rubric_from_spec(spec_text)
    console.print(f"📋 Generated rubric: {len(rubric['criteria'])} criteria (min_score={rubric['min_score']})")

    return {
        "task": task,
        "rubric": rubric,
        "iteration": 0,
        "total_cost": COST_PER_CALL["planning"],
    }


async def context_router_node(state: dict, config: RunnableConfig) -> dict:
    """Context Router: budget allocation + compression between Planning and Execution."""
    console.print("\n[bold cyan]LANGGRAPH: Context Router Node[/]")
    result = await raw_context_router(state)
    budget = result.get("context_budget", {})
    console.print(f"[green]Budget applied: {budget.get('complexity', '?')} | "
                  f"{budget.get('files_before', 0)}→{budget.get('files_after', 0)} files | "
                  f"{budget.get('tokens_before', 0)}→{budget.get('tokens_after', 0)} tok[/]")
    return result


async def model_router_node(state: dict, config: RunnableConfig) -> dict:
    """Model Router: LLM-assisted model selection with deterministic fallback."""
    console.print("\n[bold cyan]LANGGRAPH: Model Router Node[/]")
    task = cast(TaskInput, state["task"])

    # Try LLM-assisted routing via provider chain
    model_config, source, reason = await select_model_llm(task)

    if model_config is None:
        # Fallback: deterministic scoring
        model_config = select_model(task)
        console.print(f"[yellow]LLM router unavailable, deterministic fallback → {model_config['id']}[/]")
    else:
        console.print(f"[green]Model selected via {source}: {model_config['id']} ({reason})[/]")

    console.print(
        f"[dim]  {model_config['model']} | {model_config['provider']} | "
        f"max_tokens={model_config['max_tokens']}[/]"
    )
    required_caps = task.get("required_capabilities", [])
    if required_caps:
        console.print(f"[dim]  Required capabilities: {', '.join(required_caps)}[/]")
    complexity_est = task.get("complexity_estimate") or task.get("complexity", "medium")
    console.print(f"[dim]  Complexity: {complexity_est} | Tier: {task.get('tier', 'standard')}[/]")
    return {"execution_config": model_config}


async def static_gate_node(state: dict, config: RunnableConfig) -> dict:
    """Static Gate: run ruff before verification, route to retry if errors found."""
    console.print("\n[bold cyan]LANGGRAPH: Static Gate Node[/]")
    artifacts = state.get("artifacts", [])
    if not artifacts:
        return {"static_gate_passed": False, "feedback": "No artifacts to check"}

    last = artifacts[-1]
    code = last.get("code", "")
    lang = state.get("task", {}).get("language", "python")

    if not code:
        console.print("[yellow]No code in artifact, skipping static gate[/]")
        return {"static_gate_passed": True, "feedback": ""}

    result = static_gate_check(code, lang)
    if result["passed"]:
        console.print(f"[green]Static gate: PASS ({result['error_count']} errors)[/]")
        return {"static_gate_passed": True, "feedback": ""}

    console.print(f"[red]Static gate: FAIL ({result['error_count']} errors)[/]")
    console.print(f"[dim]Abstract feedback:\n{result['feedback']}[/]")
    return {"static_gate_passed": False, "feedback": result["feedback"]}


async def execution_node(state: dict, config: RunnableConfig) -> dict:
    """Execution node: TaskInput -> CodeArtifact (in a git worktree)."""
    console.print(f"\n[bold cyan]LANGGRAPH: Execution Node (iter {state.get('iteration', 0) + 1})[/]")
    task = cast(TaskInput, state["task"])
    feedback = state.get("feedback", "")

    artifact_id = uuid.uuid4().hex[:12]
    task_id = task.get("task_id", "task-000")
    wt_path = create_worktree(task_id, artifact_id)

    if feedback:
        task_with_feedback = dict(task)
        task_with_feedback["description"] = (
            f"{task['description']}\n\n"
            f"## Previous Attempt Feedback\n"
            f"{feedback}\n\n"
            f"Please address these issues in the implementation."
        )
        task = cast(TaskInput, task_with_feedback)
        console.print(f"Feedback: {feedback[:80]}...")

    start = time.time()
    raw_artifact = await execute(task)
    duration = time.time() - start

    # Write generated files into the worktree
    content = raw_artifact.get("files", {}) or {}
    if content:
        apply_diff_to_worktree(wt_path, content)
        commit_worktree(wt_path, f"feat: {task_id}")

    code = "\n".join(content.values())
    git_diff = get_diff_from_main(wt_path)
    branch_name = f"wt/{task_id}-{artifact_id}"

    artifact = {
        "artifact_id": artifact_id,
        "task_id": task_id,
        "files_changed": list(content.keys()),
        "git_diff": git_diff,
        "code": code,
        "logs": raw_artifact.get("logs", ""),
        "worktree_path": str(wt_path),
        "branch_name": branch_name,
    }

    # ── Self-Reflection ──────────────────────────────────────────────────
    reflection = await run_self_reflection(
        description=task["description"],
        code=code,
        language=task.get("language", "python"),
    )
    if not reflection["clean"]:
        console.print(f"[bold yellow]Self-reflection: {reflection['flaw_type']} flaw — regenerating[/]")
        fix_task = cast(TaskInput, {
            **task,
            "description": (
                f"{task['description']}\n\n"
                f"## Self-Reflection Fix\n"
                f"The previous version has a {reflection['flaw_type']} issue: "
                f"{reflection['fix_instruction']}\n\n"
                f"Please fix the issue and regenerate the code."
            ),
        })
        fixed_raw = await execute(fix_task)
        fixed_content = fixed_raw.get("files", {}) or {}
        fixed_code = "\n".join(fixed_content.values())
        fixed_git_diff = get_diff_from_main(wt_path)
        artifact = {
            **artifact,
            "code": fixed_code,
            "git_diff": fixed_git_diff,
            "files_changed": list(fixed_content.keys()),
        }

    console.print(f"Time: {duration:.1f}s | Cost: ${COST_PER_CALL['execution']:.3f}")
    return {
        "artifacts": [artifact],
        "iteration": 1,
        "total_cost": COST_PER_CALL["execution"],
        "current_wave": state.get("current_wave", 1),
        "pending_approval": [],
    }


async def verify_wave_node(state: dict, config: RunnableConfig) -> dict:
    """Verification node: CodeArtifact -> Verdict.
    Uses the dynamic rubric from state (generated by planning, NEVER seen by execution).
    Auto-merges high-quality code, flags borderline for human review."""
    console.print("\n[bold magenta]LANGGRAPH: Verify Wave Node[/]")
    repo_path = get_repo_path()
    rubric: VerificationRubric = state.get("rubric", {})
    artifacts = state.get("artifacts", [])
    plan_output = state.get("plan_output", {})
    completed = list(state.get("completed_tasks", []))
    pending = list(state.get("pending_approval", []))
    verification_pending = list(state.get("verification_pending", []))

    existing_ids = {v["artifact_id"] for v in state.get("verdicts", [])}
    new_verdicts = []
    for artifact in artifacts:
        artifact_id = artifact.get("artifact_id", "")
        if artifact_id in existing_ids:
            continue
        task_id = artifact.get("task_id", "unknown")
        if task_id in completed or any(p.get("task_id") == task_id for p in pending):
            continue

        start = time.time()
        verdict = await verify(artifact, rubric)
        duration = time.time() - start
        console.print(f"Time: {duration:.1f}s | Score: {verdict['score']:.2f}")
        new_verdicts.append(verdict)

        score = verdict["score"]
        consistency = verdict.get("consistency")
        wt_path = artifact.get("worktree_path", "")
        branch_name = artifact.get("branch_name", "")

        min_score = rubric.get("min_score", 0.6)
        if score > min_score + 0.2:
            completed.append(task_id)
            if wt_path and Path(wt_path).exists():
                merge_worktree(
                    Path(wt_path), branch_name,
                    plan_output.get("staging_branch", "main"),
                    repo_path=repo_path,
                )
                cleanup_worktree(Path(wt_path), delete_branch=True, branch_name=branch_name, repo_path=repo_path)
            console.print(f"[green]PASS (auto-merged)")
        elif score >= min_score:
            pending.append({"task_id": task_id, "artifact": artifact, "score": score})
            console.print(f"[yellow]Borderline ({score:.2f}), pending approval")
        else:
            verification_pending.append(task_id)
            console.print(f"[red]FAIL ({score:.2f}), will retry")

    cons_scores = [v.get("consistency") for v in new_verdicts if v.get("consistency") is not None]
    return {
        "verdicts": new_verdicts,
        "completed_tasks": completed,
        "pending_approval": pending,
        "verification_pending": verification_pending,
        "total_cost": len(new_verdicts) * COST_PER_CALL["verification"],
        "consistency_score": cons_scores[0] if cons_scores else None,
    }


CLOUD_FALLBACK_MODELS = [
    ("qwen3-coder-next:cloud", None),
    ("devstral-small-2:24b-cloud", None),
]


async def _run_with_model(
    task: TaskInput,
    task_id: str,
    language: str = "python",
    model_name: str | None = None,
    base_url: str | None = None,
    label: str = "local",
    execution_config: dict | None = None,
) -> dict:
    """Execute a task with a given model. Returns artifact directly — no verification.
    Verification is handled by verify_wave_node to avoid double verification.
    If execution_config is provided, its model/base_url/api_key override individual params."""
    raw = await execute(
        task,
        model_name=model_name,
        base_url=base_url,
        execution_config=execution_config,
    )
    diff_size = len(raw.get("git_diff", ""))
    console.print(f"[cyan]{label} model: diff={diff_size} chars[/]")
    return raw


async def execute_subtask_node(state: dict, config: RunnableConfig) -> dict:
    """Execute a subtask with local model, fallback to cloud models if diff small."""
    wave = state.get("current_wave", 1)
    console.step(f"Wave {wave} executing", advance=1)

    task = cast(TaskInput, state["task"])
    task_id = task.get("task_id", "task-000")

    if feedback := state.get("feedback", ""):
        task_with_feedback = dict(task)
        task_with_feedback["description"] = (
            f"{task['description']}\n\n"
            f"## Previous Attempt Feedback\n"
            f"{feedback}\n\n"
            f"Please address these issues in the implementation."
        )
        task = cast(TaskInput, task_with_feedback)

    lang = task.get("language", "python")

    # ── 1. Pre-execution confidence ─────────────────────────────────────
    pre_route = await evaluate_and_route(code="", task=task, pre_execution=True)
    if pre_route.action == "escalate_cloud":
        console.print(f"[bold cyan]Pre-execution: {pre_route.reason}[/]")
    else:
        console.print(f"[dim]Pre-execution: {pre_route.reason}[/]")

    # ── 2. Generation (Model Router picks the primary model) ──────────
    execution_config = state.get("execution_config")
    first_model = "local"
    if execution_config:
        first_model_label = execution_config.get("id", "routed")
        provider = execution_config.get("provider", "ollama")
        first_model = "local" if provider in ("ollama", "cloud_ollama") else "cloud"
        best_artifact = await _run_with_model(
            task, task_id, language=lang,
            execution_config=execution_config,
            label=first_model_label,
        )
    else:
        first_model = "cloud" if pre_route.action == "escalate_cloud" else "local"
        best_artifact = await _run_with_model(task, task_id, language=lang, label=first_model)
    best_model = execution_config.get("id", "local") if execution_config else first_model

    content = best_artifact.get("files", {}) or {}
    code = "\n".join(content.values())
    best_artifact["code"] = code

    # ── 3. Post-execution confidence ───────────────────────────────────
    post_route = await evaluate_and_route(code=code, task=task, retry_count=state.get("iteration", 0))
    console.print(f"[cyan]Post-execution confidence: {post_route.confidence:.2f} → {post_route.action}[/]")

    # Static gate errors logging
    static_errors = run_static_gate(code, lang)
    if static_errors:
        console.print(f"[yellow]Static gate: {len(static_errors)} issue(s)\n{format_static_errors(static_errors)}[/]")

    if post_route.action == "escalate_cloud" and first_model == "local":
        console.print(f"\n[bold yellow]Confidence {post_route.confidence:.2f} — escalating to cloud models[/]")
        for i, (model_name, base_url) in enumerate(CLOUD_FALLBACK_MODELS, 2):
            try:
                cloud_artifact = await _run_with_model(
                    task, task_id, language=lang,
                    model_name=model_name, base_url=base_url,
                    label=f"cloud-{i-1}",
                )
            except Exception as e:
                console.print(f"[red]Cloud model {model_name} failed: {e}[/]")
                continue

            cloud_content = cloud_artifact.get("files", {}) or {}
            cloud_code = "\n".join(cloud_content.values())
            cloud_artifact["code"] = cloud_code

            cloud_conf = post_execution_confidence(code=cloud_code, language=lang)
            if cloud_conf.score >= 0.8 or len(cloud_code) > len(code):
                best_artifact = cloud_artifact
                code = cloud_code
                best_model = model_name
                post_route = await evaluate_and_route(code=code, task=task, retry_count=0)
                console.print(f"[green]Cloud {model_name}: confidence {cloud_conf.score:.2f}[/]")
                break
        console.print(f"\n[bold]Cloud fallback result: {best_model} (confidence: {post_route.confidence:.2f})[/]")

    # ── 4. Self-Reflection ────────────────────────────────────────────
    if post_route.action == "accept":
        console.print(f"[green]High confidence ({post_route.confidence:.2f}) — skipping self-reflection[/]")
    else:
        console.print(f"[bold yellow]Self-reflection (confidence: {post_route.confidence:.2f})[/]")
        reflection = await run_self_reflection(
            description=task["description"],
            code=code,
            language=lang,
        )
        if not reflection["clean"]:
            console.print(f"[bold yellow]Self-reflection: {reflection['flaw_type']} — applying fix[/]")
            fix_task = cast(TaskInput, {
                **task,
                "description": (
                    f"{task['description']}\n\n"
                    f"## Self-Reflection Fix\n"
                    f"The previous version has a {reflection['flaw_type']} issue: "
                    f"{reflection['fix_instruction']}\n\n"
                    f"Please fix the issue and regenerate the code."
                ),
            })
            fixed_artifact = await _run_with_model(
                fix_task, task_id, language=lang, label="self-fix"
            )
            fixed_content = fixed_artifact.get("files", {}) or {}
            fixed_code = "\n".join(fixed_content.values())
            fixed_artifact["code"] = fixed_code
            best_artifact = fixed_artifact
            code = fixed_code
            console.print(f"[green]Self-fix applied, code: {len(fixed_code)} chars[/]")

    return {
        "artifacts": [best_artifact],
        "verdicts": [],  # verify_wave_node handles all verification
        "iteration": 1,
        "total_cost": COST_PER_CALL["execution"],
        "current_wave": wave,
    }


async def approve_verification_node(state: dict, config: RunnableConfig) -> dict:
    """Human-in-the-loop approval for borderline artifacts.
    In benchmark mode (auto=True), auto-approves and merges."""
    console.print("\n[bold yellow]LANGGRAPH: Approval Node[/]")
    repo_path = get_repo_path()
    plan_output = state.get("plan_output", {})
    pending = list(state.get("pending_approval", []))
    if not pending:
        return {"pending_approval": [], "completed_tasks": state.get("completed_tasks", [])}

    auto = config.get("configurable", {}).get("auto_approve", False) or True

    if auto:
        result = None
    else:
        console.print("[yellow]Waiting for human approval...")
        result = interrupt({
            "question": "Approve these borderline artifacts?",
            "artifacts": [p.get("artifact", {}).get("git_diff", "")[:200] for p in pending],
        })

    if result and result == "rejected":
        console.print("[red]Artifacts rejected by user")
        for p in pending:
            wt_path = p.get("artifact", {}).get("worktree_path", "")
            if wt_path and Path(wt_path).exists():
                cleanup_worktree(Path(wt_path), delete_branch=True)
        return {"pending_approval": [], "completed_tasks": state.get("completed_tasks", [])}

    completed = list(state.get("completed_tasks", []))
    for p in pending:
        task_id = p["task_id"]
        artifact = p.get("artifact", {})
        wt_path = artifact.get("worktree_path", "")
        branch_name = artifact.get("branch_name", "")
        if wt_path and Path(wt_path).exists():
            merge_worktree(
                Path(wt_path), branch_name,
                plan_output.get("staging_branch", "main"),
                repo_path=repo_path,
            )
            cleanup_worktree(Path(wt_path), delete_branch=True, branch_name=branch_name, repo_path=repo_path)
        completed.append(task_id)

    return {
        "completed_tasks": completed,
        "pending_approval": [],
        "verification_pending": [],
    }


async def optimization_node(state: dict, config: RunnableConfig) -> dict:
    """Optimization layer: analyze aggregated metrics."""
    console.print("\n[bold blue]LANGGRAPH: Optimization Node[/]")
    verdicts = state.get("verdicts", [])
    if not verdicts:
        return {"optimization_report": "No verdicts to analyze"}

    scores = [v.get("score", 0) for v in verdicts if v.get("score") is not None]
    avg_score = sum(scores) / len(scores) if scores else 0
    console.print(f"Average score across all artifacts: {avg_score:.2f}")

    report = {
        "total_artifacts": len(state.get("artifacts", [])),
        "total_verdicts": len(verdicts),
        "average_score": round(avg_score, 3),
        "completed_tasks": state.get("completed_tasks", []),
    }

    return {
        "optimization_report": str(report),
        "aggregated_verdicts": verdicts,
        "metrics_summary": report,
        "total_cost": 0.0,
    }


def should_retry(state: dict) -> Literal["retry", "done"]:
    """Conditional edge: retry on FAIL while iteration < 3."""
    if not state.get("verdicts"):
        return "retry"
    last_verdict = state["verdicts"][-1]
    iteration = state.get("iteration", 0)
    if last_verdict["passed"]:
        console.print(f"\n[bold green]PASS on iteration {iteration}. Stopping.[/]")
        return "done"
    if iteration >= 3:
        console.print("\n[bold red]Max retries (3) reached. Stopping.[/]")
        return "done"
    console.print(f"\n[bold yellow]FAIL on iteration {iteration}. Retrying...[/]")
    return "retry"


def route_from_verification(state: dict) -> Literal["approve", "retry", "done"]:
    """Route after wave verification: approve borderline, retry failed, or finish."""
    pending = state.get("pending_approval", [])
    retry_pending = state.get("verification_pending", [])
    iteration = state.get("iteration", 0)
    if pending:
        return "approve"
    if retry_pending and iteration < 3:
        return "retry"
    return "done"


def route_from_static_gate(state: dict) -> Literal["pass", "fail"]:
    """Route after static gate: pass → verification, fail → retry (skip LLM)."""
    passed = state.get("static_gate_passed", True)
    if passed:
        console.print("[green]Static gate: PASS → proceeding to verification[/]")
        return "pass"
    feedback = state.get("feedback", "Code has structural issues.")
    iteration = state.get("iteration", 0)
    console.print(f"[red]Static gate: FAIL → retry (iter {iteration})[/]")
    console.print(f"[dim]Abstract feedback: {feedback[:120]}[/]")
    # Increment iteration to prevent infinite loops
    return "fail"
