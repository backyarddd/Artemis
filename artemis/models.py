"""Shared data contracts for Artemis. Pure data, no behavior, no heavy imports.

Every subsystem imports from here so types stay consistent across the gateway,
approval router, engine, queue, and triggers.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


def new_id(prefix: str) -> str:
    """Short, sortable-ish unique id with a human prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


class ApprovalMode(str, Enum):
    """Runtime-switchable approval posture (Section 4)."""

    BYPASS = "bypass"  # auto-approve everything (catastrophe guards still fire)
    AUTO = "auto"      # approve safe, gate risky (default)
    ASK = "ask"        # gate every tool call

    @classmethod
    def coerce(cls, value: Any, default: "ApprovalMode" = None) -> "ApprovalMode":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        return default or cls.AUTO


class TaskSource(str, Enum):
    CLI = "cli"
    CHANNEL = "channel"
    CRON = "cron"
    WEBHOOK = "webhook"
    GOAL_LOOP = "goal_loop"
    AGENT = "agent"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    ALWAYS = "always"  # approve + add to persistent allowlist


@dataclass
class ChannelTarget:
    """Where a task's progress/result and approval prompts are delivered."""

    channel: str                      # adapter name: "cli", "telegram", "discord"
    conversation_id: str              # chat id / channel id / cli session id

    def key(self) -> str:
        return f"{self.channel}:{self.conversation_id}"


@dataclass
class InboundMessage:
    """A message arriving from a channel adapter into the gateway."""

    channel: str
    conversation_id: str
    sender_id: str
    text: str
    raw: Any = None

    def target(self) -> ChannelTarget:
        return ChannelTarget(self.channel, self.conversation_id)


@dataclass
class Task:
    """The single normalized unit every trigger enqueues (Section 6)."""

    prompt: Optional[str] = None
    command_ref: Optional[str] = None
    command_args: dict[str, Any] = field(default_factory=dict)
    profile: str = "default"
    source: TaskSource = TaskSource.CLI
    channel_target: Optional[ChannelTarget] = None
    approval_mode_override: Optional[ApprovalMode] = None
    allowed_tools_override: Optional[list[str]] = None
    max_turns: Optional[int] = None
    max_budget_usd: Optional[float] = None
    model: Optional[str] = None
    resume_session_id: Optional[str] = None
    sender_id: Optional[str] = None
    id: str = field(default_factory=lambda: new_id("task"))
    status: TaskStatus = TaskStatus.QUEUED
    created_at: float = field(default_factory=now)

    def describe(self) -> str:
        if self.command_ref:
            return f"/cmd {self.command_ref} {self.command_args or ''}".strip()
        text = (self.prompt or "").strip().replace("\n", " ")
        return (text[:80] + "...") if len(text) > 80 else text


@dataclass
class RunResult:
    """Terminal result of an engine run, sourced from the SDK result message."""

    final_text: str = ""
    session_id: Optional[str] = None
    cost_usd: float = 0.0
    num_turns: int = 0
    is_error: bool = False
    error: Optional[str] = None
    duration_ms: int = 0
    permission_denials: list[Any] = field(default_factory=list)


@dataclass
class ApprovalRequest:
    """Pushed to a channel when a tool call must be gated."""

    tool_name: str
    summary: str                       # one-line human summary of the action
    detail: str = ""                   # fuller input excerpt
    reason: str = ""                   # why it was gated
    pattern: Optional[str] = None      # what "Always allow" would persist
    id: str = field(default_factory=lambda: new_id("apr"))


@dataclass
class ProgressEvent:
    """A streamed step in a running task, rendered as edit-in-place progress."""

    kind: str                          # "thinking", "text", "tool_use", "tool_result", "status"
    text: str = ""
    tool_name: Optional[str] = None
