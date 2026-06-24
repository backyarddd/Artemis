"""Local interactive CLI channel.

Prints progress/results to stdout and falls back to a typed y/n/a approval
prompt through the same code path as button channels. Optional stdin loop lets
the daemon treat the terminal as an inbound channel (foreground use).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

from ..models import ApprovalDecision, ApprovalRequest, InboundMessage
from .base import ChannelAdapter

try:
    from rich.console import Console
    _console: Optional[Console] = Console()
except Exception:
    _console = None


def _out(text: str) -> None:
    if _console:
        _console.print(text)
    else:
        print(text, flush=True)


class CLIChannel(ChannelAdapter):
    name = "cli"
    supports_buttons = False

    def __init__(self, on_inbound, settings: Optional[dict] = None):
        super().__init__(on_inbound, settings)
        self._counter = 0
        self._reader: Optional[asyncio.Task] = None
        self._running = False
        self._last: dict[str, str] = {}
        self.conversation_id = (settings or {}).get("conversation_id", "local")

    async def start(self) -> None:
        self._running = True
        if (self.settings.get("stdin_loop") and sys.stdin and sys.stdin.isatty()):
            self._reader = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        self._running = False
        if self._reader:
            self._reader.cancel()
            self._reader = None

    async def _read_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception:
                break
            if not line:
                break
            text = line.strip()
            if text:
                await self.on_inbound(InboundMessage(
                    channel=self.name, conversation_id=self.conversation_id,
                    sender_id="local", text=text))

    async def send_text(self, conversation_id: str, text: str) -> Optional[str]:
        _out(text)
        self._counter += 1
        mid = str(self._counter)
        self._last[mid] = text
        return mid

    async def edit_text(self, conversation_id: str, message_id: Optional[str],
                        text: str) -> None:
        # No real in-place edit on a terminal; print only the new tail so live
        # progress reads as an append-only log rather than reprinting the block.
        prev = self._last.get(message_id or "", "")
        if text.startswith(prev):
            tail = text[len(prev):].strip("\n")
            if tail:
                _out(tail)
        else:
            _out(text)
        if message_id:
            self._last[message_id] = text

    async def ask_approval(self, conversation_id: str,
                           request: ApprovalRequest) -> ApprovalDecision:
        prompt = (f"\nAPPROVAL NEEDED [{request.tool_name}] {request.reason}\n"
                  f"  {request.summary}\n"
                  f"  Approve / Deny / Always-allow [y/n/a]? ")
        loop = asyncio.get_event_loop()
        try:
            answer = await loop.run_in_executor(None, input, prompt)
        except Exception:
            return ApprovalDecision.DENY
        a = (answer or "").strip().lower()
        if a.startswith("a"):
            return ApprovalDecision.ALWAYS
        if a.startswith("y"):
            return ApprovalDecision.APPROVE
        return ApprovalDecision.DENY
