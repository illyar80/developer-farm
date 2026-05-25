"""
LangGraph StateGraph — Developer Farm Orchestrator
---------------------------------------------------
Связывает Planning → Execution → Verification в единый граф с:
- Conditional edges (retry loop)
- Persistence (SQLite / memory)
- Streaming (astream_events)
- Time-travel debug
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from rich.console import Console
from rich.panel import Panel

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
except ImportError:
    AsyncSqliteSaver = None

from langgraph.checkpoint.memory import InMemorySaver

from graph.nodes import execution_node, planning_node, should_retry, verification_node
from graph.state import GraphState

console = Console()


def build_graph(checkpointer: Any) -> Any:
    """
    Создаёт и компилирует LangGraph StateGraph.

    Args:
        checkpointer: Экземпляр LangGraph checkpointer

    Returns:
        Compiled graph с checkpointer
    """
    builder = StateGraph(GraphState)

    builder.add_node("planning", planning_node)
    builder.add_node("execution", execution_node)
    builder.add_node("verification", verification_node)

    builder.set_entry_point("planning")
    builder.add_edge("planning", "execution")
    builder.add_edge("execution", "verification")
    builder.add_conditional_edges(
        "verification",
        should_retry,
        {
            "retry": "execution",
            "done": END,
        },
    )

    return builder.compile(checkpointer=checkpointer)


async def run_pipeline_with_langgraph(
    user_spec_path: str,
    feature_name: str = "default",
    thread_id: str | None = None,
    checkpoint_db: str = "./data/checkpoints.db",
) -> dict[str, Any]:
    """
    Запуск пайплайна через LangGraph с persistence.

    Args:
        user_spec_path: Путь к user-spec.md
        feature_name: Название фичи
        thread_id: Уникальный ID для checkpointing (auto-generated если None)
        checkpoint_db: Путь к SQLite для persistence
    """
    if thread_id is None:
        thread_id = f"feature-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    console.print(
        Panel.fit(
            f"[bold magenta]🚀 LANGGRAPH PIPELINE[/]\n"
            f"[cyan]Thread: {thread_id}[/]\n"
            f"[cyan]Spec: {user_spec_path}[/]",
            border_style="magenta",
        )
    )

    initial_state: GraphState = {
        "user_spec_path": user_spec_path,
        "feature_name": feature_name,
        "thread_id": thread_id,
    }
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
        }
    }

    console.print("\n[bold]▶️  Invoking graph...[/]\n")

    if AsyncSqliteSaver is not None:
        Path(checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
        console.print(f"[dim]Using SQLite checkpoints: {checkpoint_db}[/]")
        async with AsyncSqliteSaver.from_conn_string(checkpoint_db) as checkpointer:
            graph = build_graph(checkpointer)
            final_state = await graph.ainvoke(initial_state, config)
            persistence_label = "SQLite"
    else:
        console.print(
            "[yellow]⚠ langgraph.checkpoint.sqlite is not installed; using in-memory checkpoints[/]"
        )
        graph = build_graph(InMemorySaver())
        final_state = await graph.ainvoke(initial_state, config)
        persistence_label = "memory"

    console.print("\n" + "=" * 70)
    console.print("[bold]📊 LANGGRAPH FINAL STATE[/]")
    console.print("=" * 70)

    console.print(f"Thread ID: {thread_id}")
    console.print(f"Iterations: {final_state.get('iteration', 0)}")
    console.print(f"Artifacts: {len(final_state.get('artifacts', []))}")
    console.print(f"Verdicts: {len(final_state.get('verdicts', []))}")
    console.print(f"Total cost: ${final_state.get('total_cost', 0):.3f}")

    if final_state.get("verdicts"):
        last_verdict = final_state["verdicts"][-1]
        passed = last_verdict["passed"]
        console.print(
            f"\n[bold {'green' if passed else 'red'}]{'✅ PASS' if passed else '❌ FAIL'}[/]"
        )
        console.print(f"Final score: {last_verdict['score']:.2f}")

    console.print(f"\n[bold]💾 Checkpoint saved to {persistence_label}[/]")
    console.print(f"[dim]You can resume with thread_id={thread_id}[/]")

    return final_state


# ─── CLI Entry Point ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()

    user_spec = sys.argv[1] if len(sys.argv) > 1 else "work/mvp/user-spec.md"
    thread_id = sys.argv[2] if len(sys.argv) > 2 else None

    if not Path(user_spec).exists():
        console.print(f"[red]❌ Spec not found: {user_spec}[/]")
        sys.exit(1)

    try:
        asyncio.run(run_pipeline_with_langgraph(user_spec, thread_id=thread_id))
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️  Interrupted[/]")
        sys.exit(130)
