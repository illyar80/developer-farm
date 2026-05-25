"""
Developer Farm Dashboard Server
--------------------------------
aiohttp SSE-сервер, транслирующий LangGraph события в формат дашборда.

Endpoints:
- GET  /api/events?thread_id=X     — SSE stream событий
- POST /api/start                  — запуск нового пайплайна
- POST /api/approve                — human-in-the-loop approval
- GET  /                           — статика дашборда
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from aiohttp import web
from aiohttp_sse import sse_response
from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from rich.console import Console

from graph.reconciler import get_reconciler
from graph.graph import AsyncSqliteSaver, build_graph

load_dotenv()
console = Console()

# ─── Глобальное состояние (для MVP достаточно in-memory) ────────────────────
# В продакшене: Redis или SQLite
active_threads: dict[str, dict] = {}  # thread_id → {status, state, ...}
event_queues: dict[str, asyncio.Queue] = {}  # thread_id → Queue of events


def get_or_create_queue(thread_id: str) -> asyncio.Queue:
    if thread_id not in event_queues:
        event_queues[thread_id] = asyncio.Queue(maxsize=1000)
    return event_queues[thread_id]


# ─── Маппинг LangGraph events → Dashboard events ────────────────────────────
def map_lg_event(lg_event: dict, thread_id: str) -> dict | None:
    """Превращает сырое LangGraph событие в формат дашборда."""
    kind = lg_event.get("event")
    name = lg_event.get("name", "")
    ts = int(time.time() * 1000)

    worker_id = f"wt-{thread_id[:6]}"

    # Planning started
    if kind == "on_chain_start" and name == "planning":
        return {
            "type": "WorkerStarted",
            "worker": worker_id,
            "msg": "Planning: analyzing user spec",
            "status": "BUSY",
            "task": "Spec → Task decomposition",
            "progress": 10,
            "ts": ts,
        }

    # Execution started
    if kind == "on_chain_start" and name == "execution":
        return {
            "type": "WorkerStarted",
            "worker": worker_id,
            "msg": "Execution: generating code",
            "status": "BUSY",
            "task": "Code generation (Ollama)",
            "progress": 40,
            "ts": ts,
        }

    # Verification started
    if kind == "on_chain_start" and name == "verification":
        return {
            "type": "WorkerStarted",
            "worker": worker_id,
            "msg": "Verification: reviewing code",
            "status": "BUSY",
            "task": "Blind code review (Qwen)",
            "progress": 75,
            "ts": ts,
        }

    # Node completed
    if kind == "on_chain_end" and name in ("planning", "execution", "verification"):
        output = lg_event.get("data", {}).get("output", {})
        msg = f"{name.capitalize()} complete"

        # Извлекаем полезную информацию из output узла
        if name == "planning":
            task = output.get("task", {})
            msg = (
                f"Planned: {task.get('task_id', '?')} → {task.get('target_path', '?')}"
            )
        elif name == "execution":
            artifacts = output.get("artifacts", [])
            msg = f"Generated {len(artifacts)} artifact(s)"
        elif name == "verification":
            verdicts = output.get("verdicts", [])
            if verdicts:
                v = verdicts[-1]
                emoji = "✅" if v["passed"] else "❌"
                msg = f"Verdict: {emoji} score={v['score']:.2f}"
            else:
                msg = "Verification complete"

        return {
            "type": "WorkerCompleted" if name != "execution" else "WorkerStarted",
            "worker": worker_id,
            "msg": msg,
            "ts": ts,
        }

    # Error
    if kind == "on_chain_error":
        return {
            "type": "WorkerFailed",
            "worker": worker_id,
            "msg": f"Error: {str(lg_event.get('data', {}).get('error', 'unknown'))[:100]}",
            "ts": ts,
        }

    return None


# ─── Запуск графа в фоне с трансляцией событий ──────────────────────────────
async def run_graph_with_streaming(
    thread_id: str,
    user_spec_path: str,
    feature_name: str = "default",
):
    """Запускает LangGraph и стримит события в очередь дашборда."""
    reconciler = get_reconciler()
    reconciler.register_thread(thread_id, {"feature": feature_name})
    
    queue = get_or_create_queue(thread_id)
    checkpoint_db = "./data/checkpoints.db"

    config = {"configurable": {"thread_id": thread_id}}
    initial_state = {
        "user_spec_path": user_spec_path,
        "feature_name": feature_name,
        "thread_id": thread_id,
    }

    active_threads[thread_id] = {
        "status": "running",
        "state": initial_state,
        "started_at": time.time(),
    }

    async def stream_graph_events(graph) -> None:
        async for event in graph.astream_events(
            initial_state, config=config, version="v2"
        ):
            # Обновляем heartbeat при каждом событии
            reconciler.update_heartbeat(thread_id)
            
            dashboard_event = map_lg_event(event, thread_id)
            if dashboard_event:
                dashboard_event["thread_id"] = thread_id
                await queue.put(dashboard_event)
                console.print(
                    f"[dim]→ {dashboard_event['type']}: {dashboard_event['msg'][:60]}[/]"
                )

    try:
        if AsyncSqliteSaver is not None:
            Path(checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(checkpoint_db) as checkpointer:
                graph = build_graph(checkpointer)
                await stream_graph_events(graph)
        else:
            graph = build_graph(InMemorySaver())
            await stream_graph_events(graph)

        await queue.put(
            {
                "type": "WorkerCompleted",
                "worker": f"wt-{thread_id[:6]}",
                "msg": f"✅ Pipeline complete (thread: {thread_id})",
                "ts": int(time.time() * 1000),
                "thread_id": thread_id,
            }
        )
        active_threads[thread_id]["status"] = "completed"

    except Exception as e:
        await queue.put(
            {
                "type": "WorkerFailed",
                "worker": f"wt-{thread_id[:6]}",
                "msg": f"Pipeline failed: {str(e)[:100]}",
                "ts": int(time.time() * 1000),
                "thread_id": thread_id,
            }
        )
        active_threads[thread_id]["status"] = "failed"
        console.print(f"[red]Pipeline error: {e}[/]")


# ─── HTTP Endpoints ──────────────────────────────────────────────────────────
async def handle_index(request: web.Request) -> web.StreamResponse:
    """GET / — simple dashboard page."""
    index_path = Path(__file__).parent / "static" / "index.html"
    if not index_path.exists():
        raise web.HTTPNotFound(text="Dashboard index not found")
    return web.FileResponse(index_path)


async def handle_sse(request: web.Request) -> web.StreamResponse:
    """SSE endpoint для дашборда. Транслирует события из очереди."""
    thread_id = request.query.get("thread_id", "all")

    async with sse_response(request) as resp:
        if thread_id == "all":
            # Подписка на все активные threads
            queues = [get_or_create_queue(tid) for tid in active_threads]
            if not queues:
                # Нет активных — создаём demo queue
                queues = [get_or_create_queue("demo")]
        else:
            queues = [get_or_create_queue(thread_id)]

        # Heartbeat каждые 15 сек чтобы соединение не рвалось
        async def heartbeat():
            while True:
                await asyncio.sleep(15)
                try:
                    await resp.send(json.dumps({"type": "heartbeat"}))
                except Exception:
                    break

        heartbeat_task = asyncio.create_task(heartbeat())

        try:
            while True:
                # Читаем из всех очередей с timeout
                for queue in queues:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.1)
                        await resp.send(json.dumps(event))
                    except asyncio.TimeoutError:
                        continue
        except asyncio.CancelledError:
            pass
        finally:
            heartbeat_task.cancel()

    return resp


async def handle_start(request: web.Request) -> web.Response:
    """POST /api/start — запуск нового пайплайна."""
    data = await request.json()
    user_spec = data.get("user_spec", "work/mvp/user-spec.md")
    feature_name = data.get("feature_name", "default")
    thread_id = data.get("thread_id") or f"feat-{uuid.uuid4().hex[:8]}"

    if not Path(user_spec).exists():
        return web.json_response({"error": f"Spec not found: {user_spec}"}, status=400)

    # Запускаем граф в фоне
    asyncio.create_task(run_graph_with_streaming(thread_id, user_spec, feature_name))

    return web.json_response(
        {
            "thread_id": thread_id,
            "status": "started",
            "sse_url": f"/api/events?thread_id={thread_id}",
        }
    )


async def handle_approve(request: web.Request) -> web.Response:
    """POST /api/approve — human-in-the-loop (для будущей OPTIMIZATION layer)."""
    data = await request.json()
    thread_id = data.get("thread_id")

    if not thread_id or thread_id not in active_threads:
        return web.json_response({"error": "Unknown thread"}, status=404)

    # В MVP это просто логирование. В полной версии: graph.aupdate_state()
    console.print(f"[bold green]✅ APPROVED by human: {thread_id}[/]")

    queue = get_or_create_queue(thread_id)
    await queue.put(
        {
            "type": "WorkerCompleted",
            "worker": f"wt-{thread_id[:6]}",
            "msg": "Human approved the result",
            "ts": int(time.time() * 1000),
        }
    )

    return web.json_response({"status": "approved", "thread_id": thread_id})


async def handle_status(request: web.Request) -> web.Response:
    """GET /api/status — список активных threads."""
    return web.json_response(
        {
            "threads": {
                tid: {
                    "status": info["status"],
                    "started_at": info["started_at"],
                }
                for tid, info in active_threads.items()
            }
        }
    )


# ─── Application setup ──────────────────────────────────────────────────────
def create_app() -> web.Application:
    app = web.Application()

    app.router.add_get("/", handle_index)

    # API
    app.router.add_get("/api/events", handle_sse)
    app.router.add_post("/api/start", handle_start)
    app.router.add_post("/api/approve", handle_approve)
    app.router.add_get("/api/status", handle_status)

    # Статика дашборда (если будете хостить через тот же сервер)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.router.add_static("/static/", static_dir, name="static")

    return app


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")

    console.print("\n[bold magenta]🚀 Developer Farm Dashboard[/]")
    console.print(f"[cyan]http://{host}:{port}[/]")
    console.print("[dim]SSE: /api/events[/]")
    console.print(
        f'[dim]Start pipeline: curl -X POST http://localhost:{port}/api/start -d \'{{"user_spec":"work/mvp/user-spec.md"}}\'[/]\n'
    )

    app = create_app()
    web.run_app(app, host=host, port=port, print=None)
