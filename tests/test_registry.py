"""Command templates (render/resolve/agent-authoring), skills, and MCP."""

from __future__ import annotations

import pytest

from artemis.registry.commands import CommandRegistry, seed_starter_pack
from artemis.registry.mcp import McpRegistry
from artemis.registry.skills import SkillRegistry


def test_starter_pack_and_render(profile):
    seed_starter_pack(profile)
    reg = CommandRegistry(profile)
    names = {c.name for c in reg.list()}
    assert {"research", "ship", "standup", "track", "digest"} <= names
    out = reg.render("ship", {"scope": "fix typo in README"})
    assert "fix typo in README" in out


def test_resolve_shape(profile):
    seed_starter_pack(profile)
    reg = CommandRegistry(profile)
    resolved = reg.resolve("research", {"topic": "vector databases"})
    assert "prompt" in resolved and "vector databases" in resolved["prompt"]
    assert "allowed_tools" in resolved and "approval_mode" in resolved


def test_agent_authoring_pending_then_approve(profile):
    reg = CommandRegistry(profile)
    reg.create("autoflow", "agent authored", "Do the {thing}.", pending=True)
    pending = [c for c in reg.list() if c.pending]
    assert any(c.name == "autoflow" for c in pending)
    reg.approve("autoflow")
    active = [c for c in reg.list() if not c.pending]
    assert any(c.name == "autoflow" for c in active)


def test_never_overwrites_user_command(profile):
    reg = CommandRegistry(profile)
    reg.create("mine", "user authored", "original body")
    with pytest.raises(Exception):
        reg.create("mine", "x", "new body")  # overwrite defaults to False


def test_mcp_add_remove(profile):
    reg = McpRegistry(profile)
    reg.add("demo", {"command": "echo", "args": ["hi"]})
    assert "demo" in reg.list()
    assert reg.remove("demo") is True
    assert "demo" not in reg.list()


def test_skill_add_no_overwrite(profile):
    reg = SkillRegistry(profile)
    reg.add("greeter", content="# Greeter\nSay hi.")
    assert any(s["name"] == "greeter" for s in reg.list())
    with pytest.raises(Exception):
        reg.add("greeter", content="# different")
