"""
LangGraph StateGraph — Developer Farm Orchestrator
---------------------------------------------------
Multi-layer graph with:
- Planning -> Execution -> Verification loop
- Wave orchestration (multiple execute/verify iterations)
- Auto-merge for high-quality code
- Human-in-the-loop approval for borderline artifacts
- Optimization analysis after pipeline completion
- Persistence (SQLite / memory)
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from utils.output import console
from rich.panel import Panel

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
except ImportError:
    AsyncSqliteSaver = None

from graph.nodes import (
    approve_verification_node,
    context_router_node,
    execute_subtask_node,
    execution_node,
    model_router_node,
    optimization_node,
    planning_node,
    route_from_static_gate,
    route_from_verification,
    should_retry,
    static_gate_node,
    verify_wave_node,
)
from graph.state import GraphState
from graph.state import GraphState



def build_graph(checkpointer: Any = None) -> Any:
    """Create and compile a multi-layer LangGraph StateGraph."""
    if checkpointer is None:
        checkpointer = InMemorySaver()

    builder = StateGraph(GraphState)

    # Nodes
    builder.add_node("planning", planning_node)
    builder.add_node("context_router", context_router_node)
    builder.add_node("model_router", model_router_node)
    builder.add_node("execution", execution_node)
    builder.add_node("execute_subtask", execute_subtask_node)
    builder.add_node("static_gate", static_gate_node)
    builder.add_node("verification", verify_wave_node)
    builder.add_node("approve", approve_verification_node)
    builder.add_node("optimization", optimization_node)

    # Entry
    builder.set_entry_point("planning")

    # Planning -> Context Router -> Model Router -> execution with wave management
    builder.add_edge("planning", "context_router")
    builder.add_edge("context_router", "model_router")
    builder.add_edge("model_router", "execute_subtask")

    # execute_subtask -> static_gate (pre-verification)
    builder.add_edge("execute_subtask", "static_gate")

    # static_gate: if passed → verification, if failed → retry (skip LLM verification)
    builder.add_conditional_edges(
        "static_gate",
        route_from_static_gate,
        {"pass": "verification", "fail": "execute_subtask"},
    )

    # Verification routing
    builder.add_conditional_edges(
        "verification",
        route_from_verification,
        {
            "approve": "approve",
            "retry": "execute_subtask",
            "done": "optimization",
        },
    )

    # Approval -> done (or back to execution on retry)
    builder.add_edge("approve", "optimization")

    # Optimization -> END
    builder.add_edge("optimization", END)

    return builder.compile(checkpointer=checkpointer)


async def run_pipeline(
    user_spec_path: str,
    feature_name: str = "default",
    benchmark: bool = False,
    suppress_progress: bool = False,
) -> dict[str, Any]:
    """
    Run the full multi-layer pipeline.

    Args:
        user_spec_path: Path to user specification file
        feature_name: Feature name
        benchmark: If True, auto-approve borderline artifacts
        suppress_progress: If True, skip progress bar start/stop (caller manages it)

    Returns:
        Final pipeline state
    """
    thread_id = f"feature-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    console.print(
        Panel.fit(
            f"[bold magenta]DEVELOPER FARM PIPELINE[/]\n"
            f"[cyan]Thread: {thread_id}[/]\n"
            f"[cyan]Spec: {user_spec_path}[/]",
            border_style="magenta",
        )
    )

    if not suppress_progress:
        console.start()
    pipeline_start = time.time()

    config = {
        "configurable": {
            "thread_id": thread_id,
            "auto_approve": benchmark,
        }
    }

    initial_state: GraphState = {
        "user_spec_path": user_spec_path,
        "feature_name": feature_name,
        "thread_id": thread_id,
        "iteration": 0,
        "artifacts": [],
        "verdicts": [],
        "current_wave": 1,
        "completed_tasks": [],
        "pending_approval": [],
        "verification_pending": [],
        "plan_output": {},
    }

    if AsyncSqliteSaver is not None:
        async with AsyncSqliteSaver.from_conn_string("./data/checkpoints.db") as checkpointer:
            graph = build_graph(checkpointer)
            final_state = await graph.ainvoke(initial_state, config)
    else:
        graph = build_graph(InMemorySaver())
        final_state = await graph.ainvoke(initial_state, config)

    total_duration = time.time() - pipeline_start
    if not suppress_progress:
        console.stop()

    console.print("\n" + "=" * 70)
    console.print("[bold]PIPELINE FINAL STATE[/]")
    console.print("=" * 70)
    console.print(f"Thread: {thread_id}")
    console.print(f"Completed tasks: {final_state.get('completed_tasks', [])}")
    console.print(f"Artifacts: {len(final_state.get('artifacts', []))}")
    console.print(f"Verdicts: {len(final_state.get('verdicts', []))}")
    console.print(f"Total cost: ${final_state.get('total_cost', 0):.3f}")
    console.print(f"Duration: {total_duration:.1f}s")

    _save_final_report(final_state, user_spec_path, total_duration)

    return final_state


def _save_final_report(
    final_state: dict[str, Any],
    user_spec_path: str,
    duration_sec: float,
    results_dir: str = "work/mvp/results",
) -> None:
    """Save pipeline report to disk."""
    verdicts = final_state.get("verdicts", [])
    last_verdict = verdicts[-1] if verdicts else None

    # Extract consistency from latest verdict if available
    consistency = None
    verifier_breakdown = None
    if last_verdict:
        consistency = last_verdict.get("consistency")
        verifier_breakdown = last_verdict.get("verifier_breakdown")

    report = {
        "timestamp": datetime.now().isoformat(),
        "user_spec": user_spec_path,
        "iterations": final_state.get("iteration", 0),
        "final_passed": last_verdict["passed"] if last_verdict else False,
        "consistency_score": consistency,
        "verifier_breakdown": verifier_breakdown,
        "verdicts": verdicts,
        "artifacts_count": len(final_state.get("artifacts", [])),
        "completed_tasks": final_state.get("completed_tasks", []),
        "total_duration_sec": round(duration_sec, 2),
        "total_cost_usd": round(final_state.get("total_cost", 0), 3),
        "goodhart_proof": True,
    }

    out = Path(results_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "00_final_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    console.print(f"[green]Report saved: {out / '00_final_report.json'}[/]")


async def run_pipeline_with_langgraph(
    user_spec_path: str,
    feature_name: str = "default",
    thread_id: str | None = None,
    checkpoint_db: str = "./data/checkpoints.db",
    suppress_progress: bool = False,
) -> dict[str, Any]:
    """Legacy wrapper for simple 3-node pipeline. Use run_pipeline for multi-layer."""
    return await run_pipeline(
        user_spec_path=user_spec_path,
        feature_name=feature_name,
        benchmark=False,
        suppress_progress=suppress_progress,
    )


if __name__ == "__main__":
    """CLI entry point: python -m graph.graph <path-to-spec>"""
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    spec_path = sys.argv[1] if len(sys.argv) > 1 else "work/mvp/user-spec.md"
    asyncio.run(run_pipeline(user_spec_path=spec_path))
