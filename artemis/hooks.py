"""Catastrophe guards: PreToolUse hooks that deny mode-independently.

These fire even under bypass (Section 1.3). They are the hard floor beneath the
approval router: they block escapes from the profile tree and protect the
operator's real Claude/Hermes config from reads and writes alike.

The decision logic is a pure function (``evaluate``) so it can be unit tested
without the SDK. ``build_hooks`` wraps it in the SDK hook contract.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .profiles import Profile

# Tools that write/modify a file path.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Tools that read a path (we still protect sacred paths from reads).
READ_TOOLS = {"Read", "Glob", "Grep"}
PATH_KEYS = ("file_path", "notebook_path", "path", "filePath", "notebookPath")


def _sacred_paths() -> list[Path]:
    home = Path.home()
    return [home / ".claude", home / ".claude.json", home / ".hermes"]


def _service_dirs() -> list[Path]:
    home = Path.home()
    return [
        home / "Library" / "LaunchAgents",
        Path("/Library/LaunchDaemons"),
        Path("/Library/LaunchAgents"),
        home / ".config" / "systemd",
        Path("/etc/systemd"),
    ]


def _rc_files() -> list[Path]:
    home = Path.home()
    return [home / n for n in (".zshrc", ".bashrc", ".bash_profile", ".profile",
                               ".zprofile", ".zshenv")]


def _abspath(path: str, cwd: str) -> Path:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return Path(os.path.normpath(p))


def _within(path: Path, root: Path) -> bool:
    try:
        path = Path(os.path.normpath(str(path)))
        root = Path(os.path.normpath(str(root)))
        return path == root or root in path.parents
    except Exception:
        return False


@dataclass
class GuardResult:
    deny: bool
    reason: str = ""


@dataclass
class GuardConfig:
    allowed_roots: list[Path]
    allow_service_files: bool = False

    def sacred(self) -> list[Path]:
        return _sacred_paths()


def _check_path_write(p: Path, cfg: GuardConfig) -> GuardResult:
    for s in cfg.sacred():
        if _within(p, s):
            return GuardResult(True, f"write blocked: targets operator config {s}")
    if not cfg.allow_service_files:
        for d in _service_dirs():
            if _within(p, d):
                return GuardResult(True, f"write blocked: service unit dir {d}")
        for rc in _rc_files():
            if p == rc:
                return GuardResult(True, f"write blocked: shell rc file {rc}")
    if not any(_within(p, root) for root in cfg.allowed_roots):
        return GuardResult(True, f"write blocked: {p} is outside the Artemis profile tree")
    return GuardResult(False)


def _check_path_read(p: Path, cfg: GuardConfig) -> GuardResult:
    for s in cfg.sacred():
        if _within(p, s):
            return GuardResult(True, f"read blocked: operator config {s} is off limits")
    return GuardResult(False)


_RM_RECURSIVE = re.compile(r"\brm\b")
_DD = re.compile(r"\bdd\b")


def _check_bash(command: str, cwd: str, cfg: GuardConfig) -> GuardResult:
    cmd = command.strip()
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    # Fork bomb.
    if ":(){" in cmd.replace(" ", "") or re.search(r":\s*\|\s*:\s*&", cmd):
        return GuardResult(True, "blocked: fork bomb pattern")

    # Recursive/forced rm: deny when targeting sacred paths, outside allowed
    # roots, or filesystem roots / globs at dangerous locations.
    if "rm" in tokens:
        flags = "".join(t[1:] for t in tokens if t.startswith("-") and not t.startswith("--"))
        recursive = "r" in flags or "R" in flags or "--recursive" in tokens
        targets = [t for t in tokens[tokens.index("rm") + 1:] if not t.startswith("-")]
        for t in targets:
            if t in ("/", "/*", "~", "~/", "$HOME", "$HOME/", "*"):
                return GuardResult(True, f"blocked: rm targeting {t}")
            p = _abspath(t, cwd)
            for s in cfg.sacred():
                if _within(p, s):
                    return GuardResult(True, f"blocked: rm targeting operator config {p}")
            if recursive and not any(_within(p, root) for root in cfg.allowed_roots):
                return GuardResult(True, f"blocked: recursive rm outside workspace ({p})")

    # dd to a device or path.
    if "dd" in tokens and any(t.startswith("of=") for t in tokens):
        return GuardResult(True, "blocked: dd write")

    # Redirections / tee / mv / cp into sacred, service, or rc targets.
    redirect_targets = re.findall(r">>?\s*([^\s;|&]+)", cmd)
    extra = []
    for verb in ("mv", "cp", "tee", "install", "ln"):
        if verb in tokens:
            extra += [t for t in tokens[tokens.index(verb) + 1:] if not t.startswith("-")]
    for t in redirect_targets + extra:
        p = _abspath(t, cwd)
        res = _check_path_write(p, cfg)
        if res.deny:
            return GuardResult(True, f"blocked via bash: {res.reason}")
    return GuardResult(False)


def evaluate(tool_name: str, tool_input: dict[str, Any], cwd: str,
             cfg: GuardConfig) -> GuardResult:
    """Pure catastrophe-guard decision. Returns deny + reason, or allow."""
    if tool_name == "Bash":
        return _check_bash(str(tool_input.get("command", "")), cwd, cfg)

    if tool_name in WRITE_TOOLS:
        for k in PATH_KEYS:
            if tool_input.get(k):
                res = _check_path_write(_abspath(str(tool_input[k]), cwd), cfg)
                if res.deny:
                    return res
        # MultiEdit may carry an edits list; primary path already checked.
        return GuardResult(False)

    if tool_name in READ_TOOLS:
        for k in PATH_KEYS:
            if tool_input.get(k):
                res = _check_path_read(_abspath(str(tool_input[k]), cwd), cfg)
                if res.deny:
                    return res
        return GuardResult(False)

    return GuardResult(False)


def build_guard_config(profile: Profile, allow_service_files: bool = False,
                       extra_allowed: Optional[list[str]] = None) -> GuardConfig:
    roots = [profile.root]
    for p in (extra_allowed or []):
        roots.append(Path(os.path.expanduser(p)))
    return GuardConfig(allowed_roots=roots, allow_service_files=allow_service_files)


def build_hooks(profile: Profile, on_block: Optional[Callable[[str, str, Any], None]] = None,
                allow_service_files: bool = False,
                extra_allowed: Optional[list[str]] = None) -> dict:
    """Return a ``hooks`` dict for ClaudeAgentOptions wiring the guards.

    ``on_block(tool_name, reason, tool_input)`` is invoked for audit when a call
    is denied. Import of SDK types is local so this module stays importable for
    unit tests without the SDK installed.
    """
    from claude_agent_sdk import HookMatcher

    cfg = build_guard_config(profile, allow_service_files, extra_allowed)

    async def guard(input_data: dict, tool_use_id: Optional[str], context) -> dict:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
        cwd = input_data.get("cwd") or str(profile.workspace)
        res = evaluate(tool_name, tool_input, cwd, cfg)
        if res.deny:
            if on_block:
                try:
                    on_block(tool_name, res.reason, tool_input)
                except Exception:
                    pass
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Artemis catastrophe guard: {res.reason}",
                }
            }
        return {}

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]}
