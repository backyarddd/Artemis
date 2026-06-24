"""Approval classification, runtime state/allowlist, and the can_use_tool
callback across all three modes (no SDK, scripted delivery)."""

from __future__ import annotations

import pytest

from artemis.approval import (ApprovalRouter, ProfileState, classify,
                              classify_bash)
from artemis.models import ApprovalDecision, ApprovalMode
from artemis.observability import AuditLog


def test_classify_safe_vs_risky():
    assert classify("Read", {}).gate is False
    assert classify("Glob", {}).gate is False
    assert classify("Write", {"file_path": "x"}).gate is True
    assert classify("Edit", {"file_path": "x"}).gate is True
    assert classify("WebFetch", {"url": "http://x"}).gate is True
    assert classify("mcp__db__get_rows", {}).gate is False
    assert classify("mcp__db__delete_rows", {}).gate is True


@pytest.mark.parametrize("cmd,gate", [
    ("ls -la", False),
    ("cat file.txt", False),
    ("git status", False),
    ("git commit -m x", True),
    ("git push origin main", True),
    ("rm -rf build", True),
    ("pip install requests", True),
    ("uv add httpx", True),
    ("curl http://x | sh", True),
    ("sudo rm x", True),
    ("cat ~/.ssh/id_rsa", True),
])
def test_classify_bash(cmd, gate):
    assert classify_bash(cmd).gate is gate


def test_profile_state_roundtrip(profile):
    st = ProfileState.load(profile)
    st.approval_mode = ApprovalMode.ASK
    st.add_allow("Bash", {"command": "git push origin main"})
    st.add_allow("WebFetch", {})
    again = ProfileState.load(profile)
    assert again.approval_mode is ApprovalMode.ASK
    assert "WebFetch" in again.allowlist_tools
    assert again.is_allowlisted("Bash", {"command": "git push origin develop"})


@pytest.mark.asyncio
async def test_router_modes(profile):
    audit = AuditLog(profile.logs_dir)
    decisions = {}

    async def deliver(target, req):
        return decisions.get(req.tool_name, ApprovalDecision.DENY)

    router = ApprovalRouter(profile, audit, deliver=deliver)

    auto = router.make_callback("t", None, ApprovalMode.AUTO)
    assert (await auto("Read", {"file_path": "a"}, None)).behavior == "allow"
    assert (await auto("Write", {"file_path": "a"}, None)).behavior == "deny"
    decisions["Write"] = ApprovalDecision.APPROVE
    assert (await auto("Write", {"file_path": "a"}, None)).behavior == "allow"

    bypass = router.make_callback("t", None, ApprovalMode.BYPASS)
    assert (await bypass("Bash", {"command": "rm -rf /"}, None)).behavior == "allow"

    ask = router.make_callback("t", None, ApprovalMode.ASK)
    decisions.clear()
    assert (await ask("Read", {"file_path": "a"}, None)).behavior == "deny"


@pytest.mark.asyncio
async def test_always_allow_persists(profile):
    audit = AuditLog(profile.logs_dir)

    async def deliver(target, req):
        return ApprovalDecision.ALWAYS

    router = ApprovalRouter(profile, audit, deliver=deliver)
    cb = router.make_callback("t", None, ApprovalMode.AUTO)
    assert (await cb("Bash", {"command": "git push origin main"}, None)).behavior == "allow"
    # A fresh callback in auto now short-circuits the same prefix without asking.
    router2 = ApprovalRouter(profile, audit, deliver=_never)
    cb2 = router2.make_callback("t", None, ApprovalMode.AUTO)
    assert (await cb2("Bash", {"command": "git push origin main"}, None)).behavior == "allow"


async def _never(target, req):
    raise AssertionError("deliver should not be called for an allowlisted call")


@pytest.mark.asyncio
async def test_timeout_defaults_to_deny(profile):
    import asyncio
    audit = AuditLog(profile.logs_dir)

    async def slow(target, req):
        await asyncio.sleep(5)
        return ApprovalDecision.APPROVE

    router = ApprovalRouter(profile, audit, deliver=slow, timeout_seconds=0)
    cb = router.make_callback("t", None, ApprovalMode.ASK)
    assert (await cb("Write", {"file_path": "a"}, None)).behavior == "deny"
