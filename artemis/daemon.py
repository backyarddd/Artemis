"""Daemon supervisor: one process, many supervised async parts.

Starts the channel gateway, task queue worker, cron scheduler, optional webhook
server, and autonomous goal loop. On SIGTERM/SIGINT it stops accepting new work,
drains the running task, flushes state, and exits clean (Section 8).
"""

from __future__ import annotations

import asyncio
import signal
from typing import Optional

from .channels.base import ChannelAdapter
from .channels.cli import CLIChannel
from .channels.gateway import Gateway
from .config import GlobalConfig, ensure_api_key_env, get_secret
from .observability import get_logger
from .orchestrator import Orchestrator
from .profiles import ProfileManager
from .queue import TaskQueue


def build_adapters(config: GlobalConfig, profile_name: str, on_inbound,
                   foreground: bool = False) -> list[ChannelAdapter]:
    adapters: list[ChannelAdapter] = [
        CLIChannel(on_inbound, {"stdin_loop": foreground})
    ]
    log = get_logger("artemis.daemon")
    tg = get_secret("telegram_token", profile_name)
    if tg:
        try:
            from .channels.telegram import TelegramAdapter
            adapters.append(TelegramAdapter(on_inbound, {"token": tg}))
        except Exception:
            log.exception("telegram adapter unavailable")
    dc = get_secret("discord_token", profile_name)
    if dc:
        try:
            from .channels.discord import DiscordAdapter
            adapters.append(DiscordAdapter(on_inbound, {"token": dc}))
        except Exception:
            log.exception("discord adapter unavailable")
    return adapters


class Daemon:
    def __init__(self, profile_name: Optional[str] = None, foreground: bool = False):
        self.config = GlobalConfig.load()
        self.pm = ProfileManager()
        self.profile_name = profile_name or self.pm.active_name()
        ensure_api_key_env(self.profile_name)
        self.foreground = foreground
        self.profile = self.pm.get(self.profile_name)
        self.log = get_logger("artemis.daemon", self.profile.logs_dir, self.config.log_level)
        self._stop = asyncio.Event()

        # Auth bridge keeps the subscription token fresh inside the profile.
        self.pm.bridge_auth(self.profile)

        self.gateway = Gateway(self.config, self.profile.logs_dir)
        for adapter in build_adapters(self.config, self.profile_name,
                                      self.gateway.handle_inbound, foreground):
            self.gateway.register(adapter)

        self.orchestrator = Orchestrator(self.config, self.gateway, self.profile_name, self.pm)
        self.queue = TaskQueue(self.profile, self.orchestrator.run_task,
                               concurrency=1, log_level=self.config.log_level)
        self.orchestrator.enqueue = self.queue.enqueue
        self.orchestrator.queue = self.queue
        self.gateway.message_handler = self.orchestrator.handle_message

        self.scheduler = None
        self.goal_loop = None
        self.webhooks = None
        self.openai_server = None
        self._build_triggers()

    def _build_triggers(self) -> None:
        try:
            from .triggers.scheduler import Scheduler
            self.scheduler = Scheduler(self.profile, self.config, self.queue.enqueue)
            self.orchestrator.scheduler = self.scheduler
        except Exception:
            self.log.exception("scheduler unavailable")
        try:
            from .triggers.goal_loop import GoalLoop
            self.goal_loop = GoalLoop(self.profile, self.config, self.queue.enqueue)
        except Exception:
            self.log.exception("goal loop unavailable")
        if self.config.webhook.enabled:
            try:
                from .triggers.webhooks import WebhookServer
                self.webhooks = WebhookServer(self.profile, self.config, self.queue.enqueue)
            except Exception:
                self.log.exception("webhook server unavailable")
        if self.config.openai_server.enabled:
            try:
                from .server.openai_api import OpenAIServer
                self.openai_server = OpenAIServer(self.profile, self.config)
            except Exception:
                self.log.exception("openai server unavailable")

    async def run(self) -> None:
        self.log.info("starting daemon for profile '%s'", self.profile_name)
        await self.queue.start()
        await self.gateway.start()
        for part, name in ((self.scheduler, "scheduler"),
                           (self.goal_loop, "goal_loop"),
                           (self.webhooks, "webhooks"),
                           (self.openai_server, "openai_server")):
            if part:
                try:
                    await part.start()
                    self.log.info("started %s", name)
                except Exception:
                    self.log.exception("failed to start %s", name)

        self._install_signals()
        self.log.info("daemon up; %d channel(s)", len(self.gateway.adapters))
        await self._stop.wait()
        await self.shutdown()

    def _install_signals(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, ValueError):
                pass  # not available (e.g. Windows / non-main thread)

    async def shutdown(self) -> None:
        self.log.info("shutting down; draining queue")
        for part in (self.scheduler, self.goal_loop, self.webhooks, self.openai_server):
            if part:
                try:
                    await part.stop()
                except Exception:
                    self.log.exception("error stopping a trigger")
        try:
            await self.queue.stop(drain_timeout=self.config.approval.timeout_seconds + 30)
        except Exception:
            self.log.exception("error draining queue")
        try:
            await self.gateway.stop()
        except Exception:
            self.log.exception("error stopping gateway")
        self.log.info("daemon stopped cleanly")


async def run_daemon(profile_name: Optional[str] = None, foreground: bool = False) -> None:
    daemon = Daemon(profile_name, foreground)
    await daemon.run()
