"""Gateway: sender allowlist, inbound routing, approval delivery, progress."""

from __future__ import annotations

import pytest

from artemis.channels.base import ChannelAdapter
from artemis.channels.gateway import Gateway
from artemis.config import GlobalConfig
from artemis.models import (ApprovalDecision, ApprovalRequest, ChannelTarget,
                            InboundMessage, ProgressEvent)


class FakeAdapter(ChannelAdapter):
    name = "fake"
    supports_buttons = True

    def __init__(self, on_inbound, settings=None):
        super().__init__(on_inbound, settings)
        self.sent = []
        self.edits = []
        self.decision = ApprovalDecision.APPROVE

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_text(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return str(len(self.sent))

    async def edit_text(self, conversation_id, message_id, text):
        self.edits.append((message_id, text))

    async def ask_approval(self, conversation_id, request):
        return self.decision


def _gateway_with_fake(handler=None, allow=None):
    cfg = GlobalConfig()
    if allow is not None:
        cfg.channel_allowlists["fake"] = allow
    gw = Gateway(cfg, message_handler=handler)
    adapter = FakeAdapter(gw.handle_inbound)
    gw.register(adapter)
    return gw, adapter


@pytest.mark.asyncio
async def test_allowlist_blocks_unknown_sender():
    handled = []

    async def handler(msg):
        handled.append(msg)

    gw, adapter = _gateway_with_fake(handler, allow=["111"])
    await gw.handle_inbound(InboundMessage("fake", "c1", "999", "hello"))
    assert handled == []  # blocked
    await gw.handle_inbound(InboundMessage("fake", "c1", "111", "hello"))
    assert len(handled) == 1


@pytest.mark.asyncio
async def test_deliver_approval_routes_to_adapter():
    gw, adapter = _gateway_with_fake()
    adapter.decision = ApprovalDecision.ALWAYS
    req = ApprovalRequest(tool_name="Write", summary="write x")
    decision = await gw.deliver_approval(ChannelTarget("fake", "c1"), req)
    assert decision == ApprovalDecision.ALWAYS


@pytest.mark.asyncio
async def test_deliver_approval_no_channel_denies():
    gw, _ = _gateway_with_fake()
    req = ApprovalRequest(tool_name="Write", summary="x")
    assert await gw.deliver_approval(None, req) == ApprovalDecision.DENY


@pytest.mark.asyncio
async def test_progress_reporter_edits_in_place():
    gw, adapter = _gateway_with_fake()
    target = ChannelTarget("fake", "c1")
    rep = gw.progress(target, "Task X")
    await rep.update(ProgressEvent("tool_use", "Write notes.txt", tool_name="Write"))
    await rep.update(ProgressEvent("tool_use", "Bash ls", tool_name="Bash"))
    await rep.finish("done")
    assert len(adapter.sent) == 1   # first update sends
    assert adapter.edits            # later updates + finish edit in place
