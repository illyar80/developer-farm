"""
Reconciler — Kubernetes-style Control Loop
------------------------------------------
Фоновый процесс, который каждые 10 секунд:
1. Читает желаемое состояние из LangGraph checkpoints
2. Проверяет реальное состояние (heartbeats, процессы)
3. Исправляет расхождения (requeue, restart, cleanup)
"""

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from rich.console import Console
from rich.table import Table

console = Console()

# ─── Настройки ──────────────────────────────────────────────────────────────
RECONCILE_INTERVAL_SEC = 10
HEARTBEAT_TIMEOUT_SEC = 120
MAX_RETRIES = 3
CHECKPOINT_DB = "./data/checkpoints.db"


class Reconciler:
    """Control loop для автоматического восстановления системы."""

    def __init__(self):
        self.checkpoint_db = CHECKPOINT_DB
        self.checkpointer = None
        self.active_threads: dict[str, dict] = {}
        self.last_heartbeats: dict[str, float] = {}
        self.retry_counts: dict[str, int] = {}
        self.running = False
        self._conn = None  # ← НОВОЕ: сохраняем соединение

        # Инициализируем checkpointer правильно
        if Path(CHECKPOINT_DB).exists():
            try:
                import sqlite3

                # Создаём долгоживущее соединение
                self._conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
                self.checkpointer = SqliteSaver(self._conn)
                console.print(
                    f"[green]✅ Connected to checkpoint DB: {CHECKPOINT_DB}[/]"
                )
            except Exception as e:
                console.print(f"[yellow]⚠ Cannot connect to {CHECKPOINT_DB}: {e}[/]")
        else:
            console.print(f"[yellow]⚠ Checkpoint DB not found: {CHECKPOINT_DB}[/]")

    def register_thread(self, thread_id: str, metadata: dict = None):
        """Регистрирует новый thread для мониторинга."""
        self.active_threads[thread_id] = {
            "status": "running",
            "started_at": time.time(),
            "metadata": metadata or {},
        }
        self.last_heartbeats[thread_id] = time.time()
        self.retry_counts[thread_id] = 0
        console.print(f"[green]✅ Registered thread: {thread_id}[/]")

    def update_heartbeat(self, thread_id: str):
        """Обновляет timestamp последнего heartbeat."""
        if thread_id in self.active_threads:
            self.last_heartbeats[thread_id] = time.time()

    def get_thread_state(self, thread_id: str) -> Optional[dict]:
        """Читает состояние thread из checkpoint."""
        if self.checkpointer is None:
            return None  # Нет БД — не можем проверить

        try:
            config = {"configurable": {"thread_id": thread_id}}
            checkpoint = self.checkpointer.get(config)
            return checkpoint.get("values") if checkpoint else None
        except Exception as e:
            console.print(f"[yellow]⚠ Cannot read checkpoint for {thread_id}: {e}[/]")
            return None

    def check_health(self, thread_id: str) -> dict:
        """
        Проверяет здоровье thread.
        Возвращает: {"status": "healthy"|"stale"|"failed"|"completed", "reason": str}
        """
        if thread_id not in self.active_threads:
            return {"status": "unknown", "reason": "Thread not registered"}

        info = self.active_threads[thread_id]

        # Уже завершённые threads не проверяем
        if info["status"] in ("failed", "completed"):
            return {"status": info["status"], "reason": f"Already {info['status']}"}

        last_hb = self.last_heartbeats.get(thread_id, 0)
        time_since_hb = time.time() - last_hb

        # Проверяем heartbeat timeout
        if time_since_hb > HEARTBEAT_TIMEOUT_SEC:
            return {
                "status": "stale",
                "reason": f"No heartbeat for {time_since_hb:.0f}s (timeout: {HEARTBEAT_TIMEOUT_SEC}s)",
            }

        # Проверяем состояние в checkpoint (если есть БД)
        if self.checkpointer is not None:
            state = self.get_thread_state(thread_id)
            if state is None:
                # Thread зарегистрирован, но checkpoint нет — возможно только стартовал
                # Не помечаем как failed сразу, даём время
                if time_since_hb < 30:  # Первые 30 сек — grace period
                    return {"status": "healthy", "reason": "Starting up (grace period)"}
                return {"status": "healthy", "reason": "Running (no checkpoint yet)"}

            # Проверяем финальный статус
            verdicts = state.get("verdicts", [])
            if verdicts:
                last_verdict = verdicts[-1]
                iteration = state.get("iteration", 0)
                if last_verdict.get("passed") or iteration >= MAX_RETRIES:
                    return {"status": "completed", "reason": "Pipeline finished"}

        return {"status": "healthy", "reason": "OK"}

    async def reconcile(self):
        """Главный цикл reconciliation."""
        console.print("\n[cyan]🔄 Reconciler: checking all threads...[/]")

        for thread_id in list(self.active_threads.keys()):
            info = self.active_threads[thread_id]

            # Пропускаем уже завершённые threads
            if info["status"] in ("failed", "completed"):
                status_emoji = "✗" if info["status"] == "failed" else "✓"
                console.print(
                    f"  [dim]{status_emoji} {thread_id[:16]}... {info['status']} (skipped)[/]"
                )
                continue

            health = self.check_health(thread_id)

            if health["status"] == "healthy":
                console.print(
                    f"  [green]✓ {thread_id[:16]}... healthy ({health['reason']})[/]"
                )
                self.retry_counts[thread_id] = 0
                continue

            if health["status"] == "completed":
                console.print(f"  [dim]✓ {thread_id[:16]}... completed[/]")
                self.active_threads[thread_id]["status"] = "completed"
                continue

            if health["status"] == "stale":
                retry_count = self.retry_counts.get(thread_id, 0)
                console.print(
                    f"  [yellow]⚠ {thread_id[:16]}... STALE (retries: {retry_count}/{MAX_RETRIES})[/]"
                )
                console.print(f"    reason: {health['reason']}")

                if retry_count >= MAX_RETRIES:
                    console.print(
                        f"  [red]✗ Max retries reached. Marking as FAILED.[/]"
                    )
                    self.active_threads[thread_id]["status"] = "failed"
                    continue

                await self.resume_from_checkpoint(thread_id)
                continue

            if health["status"] == "failed":
                console.print(
                    f"  [red]✗ {thread_id[:16]}... FAILED: {health['reason']}[/]"
                )
                self.active_threads[thread_id]["status"] = "failed"

    async def resume_from_checkpoint(self, thread_id: str):
        """Восстанавливает выполнение thread с последнего checkpoint."""
        retry_count = self.retry_counts.get(thread_id, 0)
        console.print(
            f"  [cyan]→ Resuming {thread_id[:16]}... (attempt {retry_count + 1}/{MAX_RETRIES})[/]"
        )

        self.retry_counts[thread_id] = retry_count + 1

        if self.retry_counts[thread_id] >= MAX_RETRIES:
            console.print(f"  [red]✗ Max retries reached. Marking as FAILED.[/]")
            self.active_threads[thread_id]["status"] = "failed"
            return

        # Обновляем heartbeat чтобы не сработал timeout сразу
        self.last_heartbeats[thread_id] = time.time()

        # В реальной системе здесь был бы вызов:
        # from graph.graph import build_graph
        # graph = build_graph()
        # config = {"configurable": {"thread_id": thread_id}}
        # asyncio.create_task(graph.ainvoke(None, config))

        console.print(f"  [green]✓ Resumed (simulated). Heartbeat reset.[/]")

    async def run_forever(self):
        """Главный цикл reconciler."""
        self.running = True
        console.print(f"\n[bold magenta]🔄 Reconciler started[/]")
        console.print(f"   Interval: {RECONCILE_INTERVAL_SEC}s")
        console.print(f"   Heartbeat timeout: {HEARTBEAT_TIMEOUT_SEC}s")
        console.print(f"   Max retries: {MAX_RETRIES}")
        console.print(f"   Checkpoint DB: {CHECKPOINT_DB}\n")

        while self.running:
            try:
                await self.reconcile()
                await asyncio.sleep(RECONCILE_INTERVAL_SEC)
            except KeyboardInterrupt:
                console.print("\n[yellow]Reconciler stopped by user[/]")
                break
            except Exception as e:
                console.print(f"[red]Reconciler error: {e}[/]")
                import traceback

                traceback.print_exc()
                await asyncio.sleep(RECONCILE_INTERVAL_SEC)

    def stop(self):
        """Останавливает reconciler и закрывает соединения."""
        self.running = False
        if self._conn:
            try:
                self._conn.close()
                console.print("[dim]✓ Checkpoint DB connection closed[/]")
            except Exception:
                pass

    def print_status(self):
        """Выводит таблицу статуса всех threads."""
        from rich.table import Table

        table = Table(title="Reconciler Status")
        table.add_column("Thread ID", style="cyan")
        table.add_column("Status", style="green")
        table.add_column("Retries", style="yellow")
        table.add_column("Last HB", style="yellow")

        for thread_id, info in self.active_threads.items():
            last_hb = self.last_heartbeats.get(thread_id, 0)
            hb_time = (
                datetime.fromtimestamp(last_hb).strftime("%H:%M:%S")
                if last_hb
                else "never"
            )
            retry_count = self.retry_counts.get(thread_id, 0)

            status_style = {
                "running": "green",
                "completed": "dim",
                "failed": "red",
            }.get(info["status"], "white")

            table.add_row(
                thread_id[:20],
                f"[{status_style}]{info['status']}[/{status_style}]",
                f"{retry_count}/{MAX_RETRIES}",
                hb_time,
            )

        console.print(table)


# ─── Singleton instance ─────────────────────────────────────────────────────
_reconciler_instance: Optional[Reconciler] = None


def get_reconciler() -> Reconciler:
    """Возвращает singleton instance reconciler."""
    global _reconciler_instance
    if _reconciler_instance is None:
        _reconciler_instance = Reconciler()
    return _reconciler_instance


# ─── CLI Entry Point ────────────────────────────────────────────────────────
if __name__ == "__main__":
    reconciler = get_reconciler()

    # Для теста: регистрируем несколько threads
    reconciler.register_thread("test-thread-001", {"feature": "calculator"})
    reconciler.register_thread("test-thread-002", {"feature": "auth"})

    # Запускаем reconciler
    try:
        asyncio.run(reconciler.run_forever())
    except KeyboardInterrupt:
        reconciler.stop()
        reconciler.print_status()
