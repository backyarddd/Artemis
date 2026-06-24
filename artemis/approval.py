"""Three-mode approval router and the single can_use_tool callback (Section 4).

Modes (runtime switchable per profile, overridable per task/command):
  bypass  approve everything (catastrophe-guard hooks still hard-deny)
  auto    approve safe, gate risky (default)
  ask     gate every call

Gating pushes an ApprovalRequest to the task's channel and awaits a decision.
"Always allow" adds the tool or matched bash prefix to the per-profile
persistent allowlist. Every decision is audited. The persistent allowlist
short-circuits in bypass/auto; ask gates everything regardless.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .models import ApprovalDecision, ApprovalMode, ApprovalRequest, ChannelTarget
from .observability import AuditLog
from .profiles import Profile

# Tools that never need gating (read-only / orchestration meta).
SAFE_TOOLS = {"Read", "Glob", "Grep", "NotebookRead", "TodoWrite", "TodoRead",
              "Agent", "Task"}
# Tools that always gate in auto mode.
RISKY_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch",
               "WebSearch"}

_MCP_READONLY = re.compile(r"^mcp__[^_]+__(get|list|search|read|query|fetch|"
                           r"describe|show|find|status|count|view|lookup)", re.I)

# Risky bash signatures.
_GIT_MUTATE = re.compile(
    r"\bgit\s+(commit|push|pull|reset|rebase|checkout|switch|merge|tag|"
    r"cherry-pick|clean|revert|restore|stash\s+(drop|clear|pop)|branch\s+-[dD])\b")
_PKG_INSTALL = re.compile(
    r"\b(pip3?|uv|npm|pnpm|yarn|brew|apt|apt-get|cargo|gem|go|poetry|pipx)\b"
    r"[^|;&]*\b(install|add|i|uninstall|remove)\b")
_PIPE_TO_SHELL = re.compile(r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b")
_NETWORK = re.compile(r"\b(curl|wget|nc|ncat|ssh|scp|sftp|rsync|telnet|ftp)\b")
_DESTRUCTIVE = re.compile(r"\b(rm|mv|dd|shred|truncate|mkfs|chown|chmod)\b")
_CREDS = re.compile(r"(\.ssh|\.aws|\.netrc|\.env\b|id_rsa|\.pem\b|credentials|"
                    r"keychain|/secrets?\b|api[_-]?key|token|password)", re.I)
_SUDO = re.compile(r"\bsudo\b")


@dataclass
class ProfileState:
    """Mutable runtime state persisted to the profile's state.json."""

    approval_mode: ApprovalMode = ApprovalMode.AUTO
    allowlist_tools: list[str] = field(default_factory=list)
    allowlist_bash: list[str] = field(default_factory=list)
    paused: bool = False
    extra_allowed_paths: list[str] = field(default_factory=list)
    _path: Optional[Path] = None

    @classmethod
    def load(cls, profile: Profile) -> "ProfileState":
        st = cls(_path=profile.state_file)
        if profile.state_file.exists():
            try:
                raw = json.loads(profile.state_file.read_text())
                st.approval_mode = ApprovalMode.coerce(raw.get("approval_mode"), ApprovalMode.AUTO)
                st.allowlist_tools = list(raw.get("allowlist_tools", []))
                st.allowlist_bash = list(raw.get("allowlist_bash", []))
                st.paused = bool(raw.get("paused", False))
                st.extra_allowed_paths = list(raw.get("extra_allowed_paths", []))
            except Exception:
                pass
        return st

    def save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({
            "approval_mode": self.approval_mode.value,
            "allowlist_tools": self.allowlist_tools,
            "allowlist_bash": self.allowlist_bash,
            "paused": self.paused,
            "extra_allowed_paths": self.extra_allowed_paths,
        }, indent=2))

    def is_allowlisted(self, tool_name: str, tool_input: dict) -> bool:
        if tool_name == "Bash":
            # Exact normalized command only. A coarse prefix would let an
            # approved "git push" also auto-approve "git push --force ...".
            cmd = _normalize_bash(str(tool_input.get("command", "")))
            return cmd in self.allowlist_bash
        return tool_name in self.allowlist_tools

    def add_allow(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "Bash":
            cmd = _normalize_bash(str(tool_input.get("command", "")))
            if cmd and cmd not in self.allowlist_bash:
                self.allowlist_bash.append(cmd)
            self.save()
            return f"bash: {cmd[:60]}"
        if tool_name not in self.allowlist_tools:
            self.allowlist_tools.append(tool_name)
        self.save()
        return tool_name


def _normalize_bash(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip())


# ---------------------------------------------------------------------------
# Pure classification.
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    gate: bool
    reason: str = ""


def classify_bash(command: str) -> Classification:
    c = command
    if _PIPE_TO_SHELL.search(c):
        return Classification(True, "pipes a download into a shell")
    if _SUDO.search(c):
        return Classification(True, "uses sudo")
    if _GIT_MUTATE.search(c):
        return Classification(True, "mutating git command")
    if _PKG_INSTALL.search(c):
        return Classification(True, "installs or removes packages")
    if _DESTRUCTIVE.search(c):
        return Classification(True, "destructive file command (rm/mv/dd/...)")
    if _CREDS.search(c):
        return Classification(True, "touches credentials or secrets")
    if _NETWORK.search(c):
        return Classification(True, "makes a network call")
    return Classification(False, "read-only shell")


def classify(tool_name: str, tool_input: dict) -> Classification:
    """Auto-mode classification: gate risky, allow safe."""
    if tool_name in SAFE_TOOLS:
        return Classification(False, "safe tool")
    if tool_name.startswith("mcp__"):
        if _MCP_READONLY.match(tool_name):
            return Classification(False, "read-only MCP tool")
        return Classification(True, "MCP tool with side effects")
    if tool_name == "Bash":
        return classify_bash(str(tool_input.get("command", "")))
    if tool_name in RISKY_TOOLS:
        return Classification(True, f"{tool_name} modifies state or hits the network")
    return Classification(True, f"unrecognized tool {tool_name}")


def summarize(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """Human one-line summary + fuller detail for an approval prompt."""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        return f"Bash: {cmd[:80]}", cmd
    for k in ("file_path", "notebook_path", "path", "url", "pattern", "query"):
        if tool_input.get(k):
            return f"{tool_name}: {tool_input[k]}", json.dumps(tool_input, default=str)[:600]
    return tool_name, json.dumps(tool_input, default=str)[:600]


# Delivery callback: route an approval prompt to a channel and await a decision.
DeliverFn = Callable[[Optional[ChannelTarget], ApprovalRequest], Awaitable[ApprovalDecision]]


async def _deny_deliver(target, request) -> ApprovalDecision:
    return ApprovalDecision.DENY


class ApprovalRouter:
    """Builds the per-task can_use_tool callback from runtime state + delivery."""

    def __init__(self, profile: Profile, audit: AuditLog,
                 deliver: Optional[DeliverFn] = None,
                 timeout_seconds: int = 300, default_action: str = "deny"):
        self.profile = profile
        self.audit = audit
        self.deliver = deliver or _deny_deliver
        self.timeout_seconds = timeout_seconds
        self.default_action = default_action
        self.state = ProfileState.load(profile)

    def set_mode(self, mode: ApprovalMode) -> None:
        self.state.approval_mode = mode
        self.state.save()

    def make_callback(self, task_id: str, target: Optional[ChannelTarget],
                      mode_override: Optional[ApprovalMode] = None) -> Callable:
        """Return an async can_use_tool(tool_name, input_data, context)."""

        async def can_use_tool(tool_name: str, input_data: dict, context: Any):
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

            mode = mode_override or self.state.approval_mode

            def allow(reason: str, automatic: bool = True):
                self.audit.tool_decision(task_id, tool_name, "allow", mode.value,
                                         reason, input_data, automatic)
                return PermissionResultAllow()

            def deny(reason: str, automatic: bool = True):
                self.audit.tool_decision(task_id, tool_name, "deny", mode.value,
                                         reason, input_data, automatic)
                return PermissionResultDeny(message=reason)

            if mode == ApprovalMode.BYPASS:
                return allow("bypass mode")

            # Allowlist short-circuits in auto (not in ask).
            if mode == ApprovalMode.AUTO and self.state.is_allowlisted(tool_name, input_data):
                return allow("on persistent allowlist")

            if mode == ApprovalMode.AUTO:
                cls = classify(tool_name, input_data)
                if not cls.gate:
                    return allow(cls.reason)
                gate_reason = cls.reason
            else:  # ask
                gate_reason = "ask mode gates every call"

            # Build prompt and request a human decision via the channel.
            summary, detail = summarize(tool_name, input_data)
            pattern = (_normalize_bash(str(input_data.get("command", "")))
                       if tool_name == "Bash" else tool_name)
            req = ApprovalRequest(tool_name=tool_name, summary=summary,
                                  detail=detail, reason=gate_reason, pattern=pattern)
            self.audit.task_event(task_id, "approval_requested", tool=tool_name,
                                  reason=gate_reason)
            try:
                decision = await asyncio.wait_for(
                    self.deliver(target, req), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                fallback = (ApprovalDecision.APPROVE
                            if self.default_action == "allow" else ApprovalDecision.DENY)
                self.audit.task_event(task_id, "approval_timeout",
                                      tool=tool_name, default=self.default_action)
                decision = fallback

            if decision == ApprovalDecision.ALWAYS:
                added = self.state.add_allow(tool_name, input_data)
                self.audit.task_event(task_id, "allowlist_added", pattern=added)
                return allow(f"approved + allowlisted ({added})", automatic=False)
            if decision == ApprovalDecision.APPROVE:
                return allow("approved by operator", automatic=False)
            return deny("denied by operator", automatic=False)

        return can_use_tool
