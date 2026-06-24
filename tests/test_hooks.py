"""Catastrophe guards: pure decision logic and the live hook closure (deny even
under bypass), including symlink-resolved containment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from artemis.hooks import build_guard_config, build_hooks, evaluate


def test_guard_blocks_operator_config(profile):
    cfg = build_guard_config(profile)
    ws = str(profile.workspace)
    home = Path.home()
    assert evaluate("Write", {"file_path": str(home / ".claude" / "settings.json")}, ws, cfg).deny
    assert evaluate("Write", {"file_path": str(home / ".claude.json")}, ws, cfg).deny
    assert evaluate("Read", {"file_path": str(home / ".claude" / "x")}, ws, cfg).deny
    assert evaluate("Write", {"file_path": str(home / ".hermes" / "x")}, ws, cfg).deny


def test_guard_blocks_outside_tree(profile):
    cfg = build_guard_config(profile)
    ws = str(profile.workspace)
    assert evaluate("Write", {"file_path": "/etc/passwd"}, ws, cfg).deny
    assert evaluate("Write", {"file_path": str(Path.home() / "notes.txt")}, ws, cfg).deny


def test_guard_allows_workspace(profile):
    cfg = build_guard_config(profile)
    ws = str(profile.workspace)
    assert evaluate("Write", {"file_path": os.path.join(ws, "a.txt")}, ws, cfg).deny is False
    assert evaluate("Write", {"file_path": "notes.txt"}, ws, cfg).deny is False  # relative to cwd
    assert evaluate("Read", {"file_path": os.path.join(ws, "a.txt")}, ws, cfg).deny is False


def test_guard_bash_destructive(profile):
    cfg = build_guard_config(profile)
    ws = str(profile.workspace)
    assert evaluate("Bash", {"command": f"rm -rf {Path.home()}"}, ws, cfg).deny
    assert evaluate("Bash", {"command": "rm -rf /"}, ws, cfg).deny
    assert evaluate("Bash", {"command": "dd if=/dev/zero of=/dev/sda"}, ws, cfg).deny
    assert evaluate("Bash", {"command": f"echo x > {Path.home()}/.zshrc"}, ws, cfg).deny
    assert evaluate("Bash", {"command": "ls -la"}, ws, cfg).deny is False


def test_guard_env_var_expansion_bypass(profile):
    """Regression: $HOME / ${HOME} must be expanded so sacred paths can't slip
    through as literal relative tokens."""
    cfg = build_guard_config(profile)
    ws = str(profile.workspace)
    assert evaluate("Bash", {"command": "rm -rf $HOME/.claude"}, ws, cfg).deny
    assert evaluate("Bash", {"command": "rm -rf ${HOME}/.claude"}, ws, cfg).deny
    assert evaluate("Bash", {"command": "echo evil > $HOME/.claude/.credentials.json"}, ws, cfg).deny


def test_guard_cd_compound_bypass(profile):
    """Regression: cwd must be tracked across cd in compound commands."""
    cfg = build_guard_config(profile)
    ws = str(profile.workspace)
    assert evaluate("Bash", {"command": "cd ~ && rm -rf .claude"}, ws, cfg).deny
    assert evaluate("Bash", {"command": "cd /tmp ; cd ~ && rm -rf .hermes"}, ws, cfg).deny


def test_guard_bash_reads_credentials(profile):
    """Regression: arbitrary readers of operator credentials are denied."""
    cfg = build_guard_config(profile)
    ws = str(profile.workspace)
    assert evaluate("Bash", {"command": "cat ~/.claude/.credentials.json"}, ws, cfg).deny
    assert evaluate("Bash", {"command": "base64 $HOME/.claude/.credentials.json"}, ws, cfg).deny
    assert evaluate("Bash",
                    {"command": "python3 -c \"print(open('/Users/ai/.claude/.credentials.json').read())\""
                     .replace("/Users/ai", str(__import__('pathlib').Path.home()))}, ws, cfg).deny


def test_guard_rm_not_first_token(profile):
    """Regression: 'rm' appearing earlier as an argument must not mislead the
    target analysis for a later real rm."""
    cfg = build_guard_config(profile)
    ws = str(profile.workspace)
    assert evaluate("Bash", {"command": "grep -r rm . && rm -rf $HOME/.claude"}, ws, cfg).deny


@pytest.mark.asyncio
async def test_live_hook_denies_under_bypass(profile):
    """The actual hook closure denies operator-config writes regardless of mode."""
    hooks = build_hooks(profile)
    guard = hooks["PreToolUse"][0].hooks[0]
    inp = {"tool_name": "Write",
           "tool_input": {"file_path": str(Path.home() / ".claude" / "settings.json")},
           "cwd": str(profile.workspace)}
    res = await guard(inp, "tid", None)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"

    ok = {"tool_name": "Write",
          "tool_input": {"file_path": str(profile.workspace / "ok.txt")},
          "cwd": str(profile.workspace)}
    assert await guard(ok, "tid", None) == {}
