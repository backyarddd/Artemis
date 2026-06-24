"""Task queue: enqueue + run via a fake runner, persistence, and cancel."""

from __future__ import annotations

import asyncio

import pytest

from artemis.models import RunResult, Task
from artemis.queue import TaskQueue


@pytest.mark.asyncio
async def test_enqueue_runs(profile):
    ran = asyncio.Event()
    seen = []

    async def runner(task: Task) -> RunResult:
        seen.append(task.id)
        ran.set()
        return RunResult(final_text="ok")

    q = TaskQueue(profile, runner, concurrency=1)
    await q.start()
    tid = await q.enqueue(Task(prompt="hi", profile="default"))
    await asyncio.wait_for(ran.wait(), timeout=5)
    await q.stop()
    assert seen == [tid]


@pytest.mark.asyncio
async def test_persistence_survives_restart(profile):
    async def slow(task):
        await asyncio.sleep(10)
        return RunResult()

    q = TaskQueue(profile, slow, concurrency=1)
    # Enqueue without starting workers: lands in queue.json.
    tid = await q.enqueue(Task(prompt="persisted", profile="default"))
    assert q._persist_path.exists()

    ran = asyncio.Event()
    seen = []

    async def runner(task):
        seen.append(task.id)
        ran.set()
        return RunResult()

    q2 = TaskQueue(profile, runner, concurrency=1)
    await q2.start()  # should resume the persisted task
    await asyncio.wait_for(ran.wait(), timeout=5)
    await q2.stop()
    assert tid in seen


@pytest.mark.asyncio
async def test_cancel_pending(profile):
    async def runner(task):
        return RunResult()

    q = TaskQueue(profile, runner, concurrency=1)
    t = Task(prompt="x", profile="default")
    await q.enqueue(t)
    assert q.cancel(t.id) is True
