"""ChannelAdapter: the single interface every channel implements.

Core never imports a concrete channel. The gateway constructs adapters, hands
each an ``on_inbound`` coroutine, and fans outbound traffic back by adapter name.
An adapter delivers progress (send + edit-in-place), final results, and approval
prompts (inline keyboard where supported, typed fallback otherwise).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

from ..models import ApprovalDecision, ApprovalRequest, InboundMessage

OnInbound = Callable[[InboundMessage], Awaitable[None]]


class ChannelAdapter(ABC):
    #: stable adapter name, e.g. "cli", "telegram", "discord"
    name: str = "base"
    #: True if the channel renders inline-keyboard buttons for approvals
    supports_buttons: bool = False

    def __init__(self, on_inbound: OnInbound, settings: Optional[dict] = None):
        self.on_inbound = on_inbound
        self.settings = settings or {}

    @abstractmethod
    async def start(self) -> None:
        """Begin receiving inbound messages. Must not block forever."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop cleanly; release sockets/sessions."""

    @abstractmethod
    async def send_text(self, conversation_id: str, text: str) -> Optional[str]:
        """Send a message. Return a message id usable by edit_text, or None."""

    async def edit_text(self, conversation_id: str, message_id: Optional[str],
                        text: str) -> None:
        """Edit a prior message in place. Default: send a new message."""
        await self.send_text(conversation_id, text)

    @abstractmethod
    async def ask_approval(self, conversation_id: str,
                           request: ApprovalRequest) -> ApprovalDecision:
        """Prompt for an approval decision and await the operator's choice."""

    async def send_test_message(self, conversation_id: str) -> bool:
        """Used by setup/doctor to verify the channel can deliver."""
        try:
            await self.send_text(conversation_id,
                                 "Artemis test message: this channel is connected.")
            return True
        except Exception:
            return False
