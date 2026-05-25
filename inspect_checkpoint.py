#!/usr/bin/env python3
"""Просмотр сохранённых checkpoints."""

import importlib
import sqlite3
import sys
from pathlib import Path
from typing import Any

from rich.console import Console

SqliteSaver: Any | None = None
try:
    sqlite_module = importlib.import_module("langgraph.checkpoint.sqlite")
    SqliteSaver = getattr(sqlite_module, "SqliteSaver", None)
except ImportError:
    SqliteSaver = None

console = Console()

thread_id = sys.argv[1] if len(sys.argv) > 1 else None
checkpoint_db = Path("./data/checkpoints.db")

if SqliteSaver is None:
    console.print(
        "[yellow]⚠ langgraph.checkpoint.sqlite is not installed in this environment.[/]"
    )
    console.print(
        "[yellow]The graph currently falls back to in-memory checkpoints, so there is "
        "nothing persisted for a separate inspect process to read.[/]"
    )
    console.print(
        "[dim]Install the LangGraph SQLite checkpoint package if you want on-disk "
        "checkpoint inspection.[/]"
    )
    raise SystemExit(1)

if not checkpoint_db.exists():
    console.print(f"[yellow]⚠ Checkpoint DB not found: {checkpoint_db}[/]")
    console.print("[dim]Run the graph with SQLite checkpoint support enabled first.[/]")
    raise SystemExit(1)

checkpointer_conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
checkpointer = SqliteSaver(checkpointer_conn)

try:
    if thread_id:
        console.print(f"\n[bold]Thread: {thread_id}[/]\n")
        config = {"configurable": {"thread_id": thread_id}}
        checkpoints = list(checkpointer.list(config))

        if not checkpoints:
            console.print("[yellow]No checkpoints found for that thread.[/]")
            raise SystemExit(0)

        for i, cp in enumerate(checkpoints, 1):
            metadata = cp.metadata if hasattr(cp, "metadata") else cp["metadata"]
            checkpoint = (
                cp.checkpoint if hasattr(cp, "checkpoint") else cp["checkpoint"]
            )
            channel_values = checkpoint.get("channel_values", {})

            console.print(f"[cyan]Checkpoint {i}:[/]")
            console.print(f"  Node: {metadata.get('langgraph_node', 'unknown')}")
            console.print(f"  Values: {list(channel_values.keys())}")
            console.print(f"  Iteration: {channel_values.get('iteration', 0)}")
            console.print()
    else:
        console.print("[bold]All threads:[/]\n")
        conn = sqlite3.connect(str(checkpoint_db))
        try:
            cursor = conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
            )
            threads = [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

        if not threads:
            console.print("[yellow]No persisted threads found.[/]")
            raise SystemExit(0)

        for thread in threads:
            console.print(f"  • {thread}")
finally:
    checkpointer_conn.close()
