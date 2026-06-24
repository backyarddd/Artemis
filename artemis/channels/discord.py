"""Discord channel adapter built on discord.py v2.

Listens for inbound text, sends/edits progress messages, and gates approvals
with a button view (Approve/Deny/Always). The client runs as a background task
on the caller's asyncio loop.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import discord

from ..models import ApprovalDecision, ApprovalRequest, InboundMessage
from .base import ChannelAdapter, OnInbound

DISCORD_TEXT_LIMIT = 2000
READY_TIMEOUT = 30.0  # seconds to wait for the gateway handshake


class _ApprovalView(discord.ui.View):
    """Three-button approval prompt; each click resolves a future once."""

    def __init__(self, future: asyncio.Future):
        super().__init__(timeout=None)
        self._future = future
        self._add_button("Approve", discord.ButtonStyle.success, ApprovalDecision.APPROVE)
        self._add_button("Deny", discord.ButtonStyle.danger, ApprovalDecision.DENY)
        self._add_button("Always", discord.ButtonStyle.primary, ApprovalDecision.ALWAYS)

    def _add_button(self, label: str, style: "discord.ButtonStyle",
                    decision: ApprovalDecision) -> None:
        button = discord.ui.Button(label=label, style=style)

        async def callback(interaction: discord.Interaction) -> None:
            if not self._future.done():
                self._future.set_result(decision)
            for child in self.children:
                child.disabled = True
            self.stop()
            label_text = _decision_label(decision)
            try:
                await interaction.response.edit_message(
                    content=f"{interaction.message.content}\n\n{label_text}",
                    view=self,
                )
            except Exception:
                pass

        button.callback = callback
        self.add_item(button)


class DiscordAdapter(ChannelAdapter):
    name = "discord"
    supports_buttons = True

    def __init__(self, on_inbound: OnInbound, settings: Optional[dict] = None):
        super().__init__(on_inbound, settings)
        self._token: str = self.settings.get("token", "")
        self._client: Optional[discord.Client] = None
        self._task: Optional[asyncio.Task] = None
        # request id -> future resolved with the operator's decision
        self._pending: dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == client.user:
                return
            await self.on_inbound(
                InboundMessage(
                    channel=self.name,
                    conversation_id=str(message.channel.id),
                    sender_id=str(message.author.id),
                    text=message.content,
                    raw=message,
                )
            )

        self._client = client
        self._task = asyncio.create_task(client.start(self._token))
        try:
            await asyncio.wait_for(client.wait_until_ready(), timeout=READY_TIMEOUT)
        except asyncio.TimeoutError:
            # Leave the client running; callers can still attempt sends.
            pass

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
        self._client = None
        self._task = None

    async def send_text(self, conversation_id: str, text: str) -> Optional[str]:
        channel = await self._resolve_channel(conversation_id)
        if channel is None:
            return None
        msg = await channel.send(text[:DISCORD_TEXT_LIMIT])
        return str(msg.id)

    async def edit_text(self, conversation_id: str, message_id: Optional[str],
                        text: str) -> None:
        if message_id is None:
            return
        channel = await self._resolve_channel(conversation_id)
        if channel is None:
            return
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(content=text[:DISCORD_TEXT_LIMIT])

    async def ask_approval(self, conversation_id: str,
                           request: ApprovalRequest) -> ApprovalDecision:
        channel = await self._resolve_channel(conversation_id)
        if channel is None:
            return ApprovalDecision.DENY

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request.id] = fut

        view = _ApprovalView(fut)
        body = _format_prompt(request)
        await channel.send(content=body[:DISCORD_TEXT_LIMIT], view=view)
        try:
            return await fut
        finally:
            self._pending.pop(request.id, None)
            view.stop()  # reclaim the view even if the router timed us out

    async def _resolve_channel(self, conversation_id: str):
        if self._client is None:
            return None
        cid = int(conversation_id)
        channel = self._client.get_channel(cid)
        if channel is None:
            channel = await self._client.fetch_channel(cid)
        return channel


def _format_prompt(request: ApprovalRequest) -> str:
    lines = [f"Approval needed: {request.summary}"]
    if request.reason:
        lines.append(f"Reason: {request.reason}")
    if request.detail:
        lines.append(request.detail[:500])
    return "\n".join(lines)


def _decision_label(decision: ApprovalDecision) -> str:
    return {
        ApprovalDecision.APPROVE: "Approved",
        ApprovalDecision.DENY: "Denied",
        ApprovalDecision.ALWAYS: "Always allow",
    }[decision]
