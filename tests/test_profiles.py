"""Profile scaffolding, isolation of state, and config persistence."""

from __future__ import annotations

import json

from artemis.config import GlobalConfig
from artemis.profiles import ProfileManager


def test_scaffold_creates_tree(profile):
    assert profile.soul_md.exists()
    assert profile.workspace_claude_md.exists()
    assert profile.goals_md.exists()
    assert profile.settings_json.exists()
    assert json.loads(profile.mcp_json.read_text()) == {"mcpServers": {}}
    assert profile.commands_dir.is_dir()
    assert profile.workspace_skills.is_dir()


def test_scaffold_idempotent(artemis_home):
    pm = ProfileManager()
    p1 = pm.create("default", "Artemis")
    p1.soul_md.write_text("CUSTOM SOUL")
    p2 = pm.create("default", "Artemis")  # must not clobber
    assert p2.soul_md.read_text() == "CUSTOM SOUL"


def test_multiple_profiles_isolated(artemis_home):
    pm = ProfileManager()
    pm.create("work", "Worker")
    pm.create("home", "Homer")
    assert set(pm.list()) >= {"work", "home"}
    assert pm.path_for("work").root != pm.path_for("home").root


def test_active_profile_env_override(artemis_home, monkeypatch):
    pm = ProfileManager()
    pm.create("default", "A")
    pm.create("alt", "B")
    monkeypatch.setenv("ARTEMIS_PROFILE", "alt")
    assert pm.active_name() == "alt"


def test_config_roundtrip(artemis_home):
    cfg = GlobalConfig()
    cfg.default_approval_mode = "ask"
    cfg.budgets.daily_usd = 5.0
    cfg.channel_allowlists["telegram"] = ["123"]
    cfg.save()
    loaded = GlobalConfig.load()
    assert loaded.default_approval_mode == "ask"
    assert loaded.budgets.daily_usd == 5.0
    assert loaded.channel_allowlists["telegram"] == ["123"]


def test_sender_allowed(artemis_home):
    cfg = GlobalConfig()
    cfg.channel_allowlists["telegram"] = ["42"]
    assert cfg.sender_allowed("cli", "anyone") is True
    assert cfg.sender_allowed("telegram", "42") is True
    assert cfg.sender_allowed("telegram", "99") is False
    assert cfg.sender_allowed("discord", "1") is False  # unconfigured denies
