"""Async task queue with bounded concurrency and crash-safe persistence.

Every trigger normalizes to a Task and enqueues here (Section 6). A pool of
worker coroutines pulls tasks and hands each to the runner (the orchestrator).
Pending tasks are mirrored to ``queue.json`` so a restart resumes unstarted work.
Default concurrency is 1 (sequential); parallelism happens via subagents inside
a task, not across tasks.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .models import RunResult, Task, TaskStatus
from .observability import get_logger

Runner = Callable[[Task], Awaitable[RunResult]]


class TaskQueue:
    def __init__(self, profile, runner: Runner, concurrency: int = 1,
                 log_level: str = "INFO", max_pending: int = 500):
        self.profile = profile
        self.runner = runner
        self.concurrency = max(1, concurrency)
        self.max_pending = max_pending
        self._q: asyncio.Queue[Optional[Task]] = asyncio.Queue()
        self._pending: dict[str, Task] = {}
        self._running: dict[str, Task] = {}
        self._workers: list[asyncio.Task] = []
        self._accepting = True
        self._lock = asyncio.Lock()
        self.log = get_logger("artemis.queue", profile.logs_dir, log_level)
        self._persist_path: Path = profile.root / "queue.json"

    # ----- persistence -----------------------------------------------------

    def _save(self) -> None:
        try:
            data = [t.to_dict() for t in self._pending.values()]
            self._persist_path.write_text(json.dumps(data))
        except Exception:
            self.log.exception("failed to persist queue")

    def _load_pending(self) -> list[Task]:
        if not self._persist_path.exists():
            return []
        try:
            return [Task.from_dict(d) for d in json.loads(self._persist_path.read_text())]
        except Exception:
            self.log.exception("failed to load persisted queue")
            return []

    # ----- public api ------------------------------------------------------

    async def enqueue(self, task: Task) -> str:
        if not self._accepting:
            raise RuntimeError("queue is shutting down; not accepting tasks")
        if len(self._pending) >= self.max_pending:
            raise RuntimeError(f"queue is full ({self.max_pending} pending); backpressure")
        async with self._lock:
            task.status = TaskStatus.QUEUED
            self._pending[task.id] = task
            self._save()
        await self._q.put(task)
        self.log.info("enqueued %s (%s)", task.id, task.describe())
        return task.id

    async def start(self) -> None:
        for task in self._load_pending():
            self._pending[task.id] = task
            await self._q.put(task)
        if self._pending:
            self.log.info("resumed %d persisted task(s)", len(self._pending))
        self._workers = [asyncio.create_task(self._worker(i), name=f"queue-worker-{i}")
                         for i in range(self.concurrency)]

    async def stop(self, drain_timeout: float = 30.0) -> None:
        """Stop accepting; let in-flight tasks finish, then cancel workers."""
        self._accepting = False
        for _ in self._workers:
            await self._q.put(None)  # poison pills
        if self._workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._workers, return_exceptions=True),
                    timeout=drain_timeout)
            except asyncio.TimeoutError:
                self.log.warning("drain timed out; cancelling workers")
                for w in self._workers:
                    w.cancel()
                # Await cancellation so finally-blocks run and state is flushed.
                await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    def cancel(self, task_id: str) -> bool:
        """Cancel a not-yet-started task. Running tasks are stopped via the
        orchestrator's interrupt path, not here."""
        task = self._pending.get(task_id)
        if task and task_id not in self._running:
            task.status = TaskStatus.CANCELLED
            self._pending.pop(task_id, None)
            self._save()
            return True
        return False

    def snapshot(self) -> dict:
        return {
            "pending": [t.describe() for tid, t in self._pending.items() if tid not in self._running],
            "running": [t.describe() for t in self._running.values()],
            "accepting": self._accepting,
            "concurrency": self.concurrency,
        }

    # ----- worker ----------------------------------------------------------

    async def _worker(self, idx: int) -> None:
        while True:
            task = await self._q.get()
            try:
                if task is None:
                    return
                if task.id not in self._pending:  # cancelled before start
                    continue
                self._running[task.id] = task
                task.status = TaskStatus.RUNNING
                self.log.info("worker %d running %s", idx, task.id)
                try:
                    await self.runner(task)
                    task.status = TaskStatus.COMPLETED
                except Exception:
                    task.status = TaskStatus.FAILED
                    self.log.exception("task %s failed in runner", task.id)
                finally:
                    self._running.pop(task.id, None)
                    async with self._lock:
                        self._pending.pop(task.id, None)
                        self._save()
            finally:
                self._q.task_done()
