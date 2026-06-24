"""Cron trigger: APScheduler jobs that build CRON Tasks and enqueue them.

Jobs persist to ``<profile.root>/crons.json`` (self-contained, not the sqlite
db). Each entry is a plain dict so the catalog survives restarts. On start we
load the file and register every job live with an AsyncIOScheduler.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import GlobalConfig
from ..models import (ApprovalMode, ChannelTarget, Task, TaskSource, new_id)
from ..observability import get_logger
from ..profiles import Profile

Enqueue = Callable[[Task], Awaitable[str]]


class Scheduler:
    """APScheduler cron source. Fires build a Task(source=CRON) and enqueue it."""

    def __init__(self, profile: Profile, config: GlobalConfig, enqueue: Enqueue):
        self.profile = profile
        self.config = config
        self.enqueue = enqueue
        self.log = get_logger("artemis.scheduler", profile.logs_dir, config.log_level)
        self._sched = AsyncIOScheduler()
        self._jobs: list[dict[str, Any]] = self._load()
        self._running = False

    # ----- persistence -----------------------------------------------------

    @property
    def _path(self) -> Path:
        return self.profile.root / "crons.json"

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
            return data if isinstance(data, list) else []
        except Exception:
            self.log.exception("failed to load crons.json")
            return []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._jobs, indent=2))
        except Exception:
            self.log.exception("failed to persist crons.json")

    # ----- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        for job in self._jobs:
            self._register(job)
        self._sched.start()
        self._running = True
        self.log.info("scheduler started with %d job(s)", len(self._jobs))

    async def stop(self) -> None:
        if self._running:
            self._sched.shutdown(wait=False)
            self._running = False

    # ----- job management --------------------------------------------------

    def add_job(self, cron: str, prompt: Optional[str] = None,
                command_ref: Optional[str] = None,
                command_args: Optional[dict[str, Any]] = None,
                channel: Optional[str] = None,
                conversation_id: Optional[str] = None,
                approval_mode: Optional[str] = None,
                name: Optional[str] = None) -> str:
        # Validate the cron expression up front; raises on a bad expr.
        CronTrigger.from_crontab(cron)
        job_id = new_id("cron")
        entry: dict[str, Any] = {
            "id": job_id,
            "name": name or (command_ref or (prompt or "")[:40]),
            "cron": cron,
            "prompt": prompt,
            "command_ref": command_ref,
            "command_args": command_args or {},
            "channel": channel,
            "conversation_id": conversation_id,
            "approval_mode": approval_mode,
        }
        self._jobs.append(entry)
        self._save()
        if self._running:
            self._register(entry)
        self.log.info("added cron job %s (%s)", job_id, cron)
        return job_id

    def remove_job(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j.get("id") != job_id]
        if len(self._jobs) == before:
            return False
        self._save()
        if self._running:
            try:
                self._sched.remove_job(job_id)
            except Exception:
                pass
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        return [dict(j) for j in self._jobs]

    # ----- internals -------------------------------------------------------

    def _register(self, entry: dict[str, Any]) -> None:
        trigger = CronTrigger.from_crontab(entry["cron"])
        self._sched.add_job(self._fire, trigger=trigger, args=[entry],
                            id=entry["id"], replace_existing=True)

    async def _fire(self, entry: dict[str, Any]) -> None:
        try:
            channel = entry.get("channel")
            conv = entry.get("conversation_id")
            target = (ChannelTarget(channel, conv)
                      if channel and conv else None)
            mode = (ApprovalMode.coerce(entry["approval_mode"])
                    if entry.get("approval_mode") else None)
            task = Task(
                prompt=entry.get("prompt"),
                command_ref=entry.get("command_ref"),
                command_args=entry.get("command_args") or {},
                profile=self.profile.name,
                source=TaskSource.CRON,
                channel_target=target,
                approval_mode_override=mode,
            )
            await self.enqueue(task)
        except Exception:
            self.log.exception("cron job %s failed to fire", entry.get("id"))
