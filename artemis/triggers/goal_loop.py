"""Goal loop trigger: the autonomous heartbeat.

Every ``interval_minutes`` it wakes, and unless paused or disabled, advances the
next standing goal from the profile's goals.md (round-robin). Every
``reflect_every`` cycles it enqueues a reflection task instead. The loop is
fully guarded so one bad cycle never kills the heartbeat.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from ..approval import ProfileState
from ..config import GlobalConfig
from ..models import Task, TaskSource
from ..observability import get_logger
from ..profiles import Profile

Enqueue = Callable[[Task], Awaitable[str]]

REFLECT_PROMPT = ("Review your recent task outcomes and update your memory with "
                  "any durable lessons. Note what is working and what is stuck.")


def parse_goals(text: str) -> list[str]:
    """Actionable goals: "- " lines that are not blank, headings, or comments."""
    goals: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if not body or body.startswith("#"):
            continue
        goals.append(body)
    return goals


class GoalLoop:
    """Autonomous heartbeat. start() launches the loop; stop() cancels it."""

    def __init__(self, profile: Profile, config: GlobalConfig, enqueue: Enqueue):
        self.profile = profile
        self.config = config
        self.enqueue = enqueue
        self.log = get_logger("artemis.goal_loop", profile.logs_dir, config.log_level)
        self._task: asyncio.Task | None = None
        self._cycle = 0       # total cycles run
        self._goal_idx = 0    # round-robin cursor across goals

    # ----- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="artemis-goal-loop")
            self.log.info("goal loop started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ----- loop ------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            # Read interval each iteration so config changes take effect live.
            interval = max(1, int(self.config.goal_loop.interval_minutes))
            await asyncio.sleep(interval * 60)
            try:
                await self._tick()
            except Exception:
                self.log.exception("goal loop cycle failed")

    async def _tick(self) -> None:
        if not self.config.goal_loop.enabled:
            return
        if ProfileState.load(self.profile).paused:
            self.log.info("goal loop paused; skipping cycle")
            return

        self._cycle += 1
        reflect_every = max(1, int(self.config.goal_loop.reflect_every))
        if self._cycle % reflect_every == 0:
            await self._enqueue_prompt(REFLECT_PROMPT)
            return

        goals = self._read_goals()
        if not goals:
            self.log.info("no actionable goals; skipping cycle")
            return
        goal = goals[self._goal_idx % len(goals)]
        self._goal_idx += 1
        prompt = ("Advance this standing goal. Take one concrete step, then "
                  f"report what you did and what remains.\n\nGoal: {goal}")
        await self._enqueue_prompt(prompt)

    def _read_goals(self) -> list[str]:
        try:
            return parse_goals(self.profile.goals_md.read_text())
        except Exception:
            return []

    async def _enqueue_prompt(self, prompt: str) -> None:
        task = Task(prompt=prompt, profile=self.profile.name,
                    source=TaskSource.GOAL_LOOP)
        await self.enqueue(task)
