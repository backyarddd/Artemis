"""Catastrophe guards: PreToolUse hooks that deny mode-independently.

These fire even under bypass (Section 1.3). They are the hard floor beneath the
approval router: they block escapes from the profile tree and protect the
operator's real Claude/Hermes config from reads and writes alike.

The decision logic is a pure function (``evaluate``) so it can be unit tested
without the SDK. ``build_hooks`` wraps it in the SDK hook contract.
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .profiles import Profile

# macOS (APFS default) and Windows are case-insensitive; compare case-folded.
_CASE_INSENSITIVE = platform.system() in ("Darwin", "Windows")

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
    # Expand BOTH ~ and environment variables so the guard sees what the shell
    # will actually target (e.g. $HOME, ${HOME}). realpath resolves symlinks
    # (macOS /tmp -> /private/tmp) and tolerates a non-existent leaf.
    p = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return Path(os.path.realpath(p))


def _canon(p) -> str:
    """Canonical comparison form: realpath + NFC unicode + case fold where the
    filesystem is case-insensitive. Defeats .Claude / NFD-decomposed aliasing."""
    s = os.path.realpath(os.path.expanduser(str(p)))
    s = unicodedata.normalize("NFC", s)
    return s.casefold() if _CASE_INSENSITIVE else s


def _within(path: Path, root: Path) -> bool:
    try:
        cp, cr = _canon(path), _canon(root)
        return cp == cr or cp.startswith(cr.rstrip("/") + "/")
    except Exception:
        return False


def _references_sacred(text: str, cfg: "GuardConfig") -> Optional[str]:
    """True if a (possibly interpreter-embedded) command string names a sacred
    path after ~ and env-var expansion. Catches readers like cat/python/base64."""
    expanded = os.path.expandvars(os.path.expanduser(text))
    hay = expanded.casefold() if _CASE_INSENSITIVE else expanded
    for s in cfg.sacred():
        needle = str(s).casefold() if _CASE_INSENSITIVE else str(s)
        if needle in hay:
            return f"references operator config {s}"
    return None


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


_STMT_SPLIT = re.compile(r"&&|\|\||;|\||\n|&")
_DESTRUCTIVE_VERBS = {"rm", "rmdir", "dd", "shred", "truncate", "mv", "cp",
                      "tee", "install", "ln"}
_REDIRECT = re.compile(r">>?\s*([^\s;|&>]+)")
_DANGEROUS_RM_LITERALS = {"/", "/*", "*", "~", "~/", "."}


def _check_bash(command: str, cwd: str, cfg: GuardConfig) -> GuardResult:
    """Conservative bash guard.

    Parsing arbitrary shell safely is impossible, so this errs toward denial:
    it expands ~ and env vars, scans for any reference to a sacred path
    (catching readers like cat/python), tracks cwd across `cd` in compound
    commands, and refuses destructive statements that still contain unresolved
    expansion. Risky-but-benign commands are still gated by the approval router;
    this layer only blocks catastrophes.
    """
    cmd = command.strip()
    if ":(){" in cmd.replace(" ", "") or re.search(r":\s*\|\s*:\s*&", cmd):
        return GuardResult(True, "fork bomb pattern")

    # Catch-all: any sacred path named anywhere (incl. inside interpreter args).
    ref = _references_sacred(cmd, cfg)
    if ref:
        return GuardResult(True, ref)

    current = cwd or os.getcwd()
    for raw in _STMT_SPLIT.split(cmd):
        stmt = raw.strip()
        if not stmt:
            continue
        expanded = os.path.expandvars(os.path.expanduser(stmt))
        try:
            toks = shlex.split(expanded)
        except ValueError:
            toks = expanded.split()
        if not toks:
            continue
        verb = os.path.basename(toks[0])
        args = toks[1:]
        nonflag = [t for t in args if not t.startswith("-")]

        if verb == "cd":
            if nonflag:
                current = str(_abspath(nonflag[0], current))
            continue

        # Any token resolving inside a sacred path (relative to the tracked cwd).
        for t in nonflag:
            p = _abspath(t, current)
            for s in cfg.sacred():
                if _within(p, s):
                    return GuardResult(True, f"targets operator config {s}")

        destructive = verb in _DESTRUCTIVE_VERBS or _REDIRECT.search(stmt)
        # Unresolved expansion in a destructive statement is unanalyzable: deny.
        if destructive and ("`" in stmt or "$(" in stmt or re.search(r"\$\w+|\$\{", expanded)):
            return GuardResult(True, "unresolved shell expansion in a destructive command")

        if verb in ("rm", "rmdir", "shred"):
            flags = "".join(t[1:] for t in args if t.startswith("-") and not t.startswith("--"))
            recursive = "r" in flags or "R" in flags or "--recursive" in args
            for t in nonflag:
                if t in _DANGEROUS_RM_LITERALS:
                    return GuardResult(True, f"rm targeting {t}")
                p = _abspath(t, current)
                if recursive and not any(_within(p, root) for root in cfg.allowed_roots):
                    return GuardResult(True, f"recursive delete outside workspace ({p})")

        if verb == "dd" and any(t.startswith("of=") for t in args):
            return GuardResult(True, "dd write")

        for rt in _REDIRECT.findall(stmt):
            res = _check_path_write(_abspath(rt, current), cfg)
            if res.deny:
                return GuardResult(True, f"redirect {res.reason}")

        if verb in ("mv", "cp", "tee", "install", "ln"):
            for t in nonflag:
                res = _check_path_write(_abspath(t, current), cfg)
                if res.deny:
                    return GuardResult(True, res.reason)

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
