"""Telegram channel adapter built on python-telegram-bot v22+.

Polls for inbound text, sends/edits progress messages, and gates approvals with
an inline keyboard (Approve/Deny/Always). All work runs on the caller's running
asyncio loop; no loop is created here.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..models import ApprovalDecision, ApprovalRequest, InboundMessage
from .base import ChannelAdapter, OnInbound

TELEGRAM_TEXT_LIMIT = 4096
CALLBACK_DATA_LIMIT = 64  # Telegram callback_data hard limit (bytes)


class TelegramAdapter(ChannelAdapter):
    name = "telegram"
    supports_buttons = True

    def __init__(self, on_inbound: OnInbound, settings: Optional[dict] = None):
        super().__init__(on_inbound, settings)
        self._token: str = self.settings.get("token", "")
        self._app: Optional[Application] = None
        # request id -> future resolved with the operator's decision
        self._pending: dict[str, asyncio.Future] = {}
        # short callback token -> request id (callback_data must stay < 64 bytes)
        self._token_map: dict[str, str] = {}

    async def start(self) -> None:
        app = ApplicationBuilder().token(self._token).build()
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )
        app.add_handler(CallbackQueryHandler(self._on_callback))
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        self._app = app

    async def stop(self) -> None:
        app = self._app
        if app is None:
            return
        try:
            if app.updater is not None:
                await app.updater.stop()
        except Exception:
            pass
        try:
            await app.stop()
        except Exception:
            pass
        try:
            await app.shutdown()
        except Exception:
            pass
        self._app = None

    async def send_text(self, conversation_id: str, text: str) -> Optional[str]:
        if self._app is None:
            return None
        msg = await self._app.bot.send_message(
            chat_id=int(conversation_id), text=text[:TELEGRAM_TEXT_LIMIT]
        )
        return str(msg.message_id)

    async def edit_text(self, conversation_id: str, message_id: Optional[str],
                        text: str) -> None:
        if self._app is None or message_id is None:
            return
        try:
            await self._app.bot.edit_message_text(
                chat_id=int(conversation_id),
                message_id=int(message_id),
                text=text[:TELEGRAM_TEXT_LIMIT],
            )
        except BadRequest as exc:
            # Editing to identical content is a no-op error; ignore it.
            if "not modified" not in str(exc).lower():
                raise

    async def ask_approval(self, conversation_id: str,
                           request: ApprovalRequest) -> ApprovalDecision:
        token = self._short_token(request.id)
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Approve", callback_data=f"{token}:approve"),
                InlineKeyboardButton("Deny", callback_data=f"{token}:deny"),
                InlineKeyboardButton("Always", callback_data=f"{token}:always"),
            ]]
        )
        body = self._format_prompt(request)
        msg = await self._app.bot.send_message(
            chat_id=int(conversation_id),
            text=body[:TELEGRAM_TEXT_LIMIT],
            reply_markup=keyboard,
        )

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request.id] = fut
        try:
            decision: ApprovalDecision = await fut
        finally:
            self._pending.pop(request.id, None)
            self._token_map.pop(token, None)

        try:
            await self._app.bot.edit_message_text(
                chat_id=int(conversation_id),
                message_id=msg.message_id,
                text=f"{body}\n\n{self._decision_label(decision)}"[:TELEGRAM_TEXT_LIMIT],
            )
        except Exception:
            pass
        return decision

    async def _on_message(self, update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        message = update.message
        if chat is None or message is None or message.text is None:
            return
        await self.on_inbound(
            InboundMessage(
                channel=self.name,
                conversation_id=str(chat.id),
                sender_id=str(user.id) if user is not None else "",
                text=message.text,
                raw=update,
            )
        )

    async def _on_callback(self, update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        token, _, choice = query.data.partition(":")
        request_id = self._token_map.get(token)
        try:
            await query.answer()
        except Exception:
            pass
        if request_id is None:
            return
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return
        try:
            decision = ApprovalDecision(choice)
        except ValueError:
            return
        fut.set_result(decision)

    def _short_token(self, request_id: str) -> str:
        # Keep callback_data well under the 64-byte cap by mapping to a token.
        token = request_id
        if len(f"{token}:always") > CALLBACK_DATA_LIMIT:
            token = f"a{len(self._token_map)}"
        self._token_map[token] = request_id
        return token

    @staticmethod
    def _format_prompt(request: ApprovalRequest) -> str:
        lines = [f"Approval needed: {request.summary}"]
        if request.reason:
            lines.append(f"Reason: {request.reason}")
        if request.detail:
            lines.append(request.detail[:500])
        return "\n".join(lines)

    @staticmethod
    def _decision_label(decision: ApprovalDecision) -> str:
        return {
            ApprovalDecision.APPROVE: "Approved",
            ApprovalDecision.DENY: "Denied",
            ApprovalDecision.ALWAYS: "Always allow",
        }[decision]
