"""The single gateway: one process fans out to many channels.

Core never references a concrete channel; the gateway owns the adapter set and
routes by name. It enforces the per-channel sender allowlist on inbound, drives
edit-in-place progress on outbound, and supplies the approval router's delivery
function (route an ApprovalRequest to the right channel and await the tap).
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from ..config import GlobalConfig
from ..models import (ApprovalDecision, ApprovalRequest, ChannelTarget,
                      InboundMessage, ProgressEvent)
from ..observability import get_logger
from .base import ChannelAdapter

MessageHandler = Callable[[InboundMessage], Awaitable[None]]


class ProgressReporter:
    """Edit-in-place progress for one task on one channel target.

    First update sends a message; later updates edit it. Keeps a compact rolling
    view (header + last few steps) so long runs do not spam the channel.
    """

    def __init__(self, gateway: "Gateway", target: Optional[ChannelTarget],
                 header: str, max_steps: int = 8):
        self.gateway = gateway
        self.target = target
        self.header = header
        self.max_steps = max_steps
        self._steps: list[str] = []
        self._message_id: Optional[str] = None
        self._final = False

    def _render(self) -> str:
        body = "\n".join(self._steps[-self.max_steps:])
        return f"{self.header}\n{body}".strip()

    async def update(self, event: ProgressEvent) -> None:
        if self._final or self.target is None:
            return
        line = self._format(event)
        if not line:
            return
        self._steps.append(line)
        await self._flush()

    async def _flush(self) -> None:
        text = self._render()
        try:
            if self._message_id is None:
                self._message_id = await self.gateway.send(self.target, text)
            else:
                await self.gateway.edit(self.target, self._message_id, text)
        except Exception:
            self.gateway.log.debug("progress flush failed", exc_info=True)

    async def finish(self, summary: str) -> None:
        if self.target is None:
            return
        self._final = True
        text = f"{self.header}\n{summary}".strip()
        try:
            if self._message_id is None:
                await self.gateway.send(self.target, text)
            else:
                await self.gateway.edit(self.target, self._message_id, text)
        except Exception:
            self.gateway.log.debug("progress finish failed", exc_info=True)

    @staticmethod
    def _format(event: ProgressEvent) -> str:
        if event.kind == "tool_use":
            return f"  -> {event.text}"
        if event.kind == "thinking":
            return "  (thinking)"
        if event.kind == "text" and event.text.strip():
            t = event.text.strip().replace("\n", " ")
            return f"  {t[:140]}"
        return ""


class Gateway:
    def __init__(self, config: GlobalConfig, logs_dir=None,
                 message_handler: Optional[MessageHandler] = None):
        self.config = config
        self.adapters: dict[str, ChannelAdapter] = {}
        self.message_handler = message_handler
        self.log = get_logger("artemis.gateway", logs_dir, config.log_level)

    def register(self, adapter: ChannelAdapter) -> None:
        self.adapters[adapter.name] = adapter
        self.log.info("registered channel %s", adapter.name)

    def has(self, channel: str) -> bool:
        return channel in self.adapters

    # ----- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        for adapter in self.adapters.values():
            try:
                await adapter.start()
                self.log.info("started channel %s", adapter.name)
            except Exception:
                self.log.exception("channel %s failed to start", adapter.name)

    async def stop(self) -> None:
        for adapter in self.adapters.values():
            try:
                await adapter.stop()
            except Exception:
                self.log.exception("channel %s failed to stop", adapter.name)

    # ----- inbound ---------------------------------------------------------

    async def handle_inbound(self, msg: InboundMessage) -> None:
        """Adapters route inbound here (gateway sets each adapter.on_inbound)."""
        if not self.config.sender_allowed(msg.channel, msg.sender_id):
            self.log.warning("blocked inbound from %s:%s (not allowlisted)",
                             msg.channel, msg.sender_id)
            try:
                await self.send(msg.target(),
                                "You are not on this agent's allowlist.")
            except Exception:
                pass
            return
        if self.message_handler:
            await self.message_handler(msg)

    # ----- outbound --------------------------------------------------------

    async def send(self, target: Optional[ChannelTarget], text: str) -> Optional[str]:
        if target is None or target.channel not in self.adapters:
            return None
        return await self.adapters[target.channel].send_text(target.conversation_id, text)

    async def edit(self, target: ChannelTarget, message_id: Optional[str], text: str) -> None:
        if target.channel in self.adapters:
            await self.adapters[target.channel].edit_text(target.conversation_id, message_id, text)

    def progress(self, target: Optional[ChannelTarget], header: str) -> ProgressReporter:
        return ProgressReporter(self, target, header)

    # ----- approval delivery (used by approval.ApprovalRouter) -------------

    async def deliver_approval(self, target: Optional[ChannelTarget],
                               request: ApprovalRequest) -> ApprovalDecision:
        if target is None or target.channel not in self.adapters:
            # No channel to ask: deny by default (router still audits).
            return ApprovalDecision.DENY
        return await self.adapters[target.channel].ask_approval(
            target.conversation_id, request)
