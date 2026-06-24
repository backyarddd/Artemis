"""Engine: build ClaudeAgentOptions for a task + profile, run, route the stream.

Isolation is enforced here (Section 1.1): CLAUDE_CONFIG_DIR relocation, strict
MCP load, scoped setting_sources, profile settings, profile workspace as cwd.
The approval router supplies can_use_tool and catastrophe guards supply hooks;
both are injected so the engine stays a thin, well-typed wrapper.

can_use_tool requires streaming mode, so we drive ClaudeSDKClient (not one-shot
query). The system prompt extends the claude_code preset with the profile soul
plus any recalled memory, rather than replacing it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .config import GlobalConfig
from .models import ProgressEvent, RunResult, Task
from .observability import AuditLog, CostTracker, get_logger
from .profiles import Profile

DEFAULT_MAX_TURNS = 60
# setting_sources to load. CLAUDE_CONFIG_DIR relocates "user" to the profile
# claude-home, so "user" loads Artemis's own skills, not the operator's;
# "project" loads the workspace .claude. The operator's real config is never
# referenced. doctor proves this empirically.
DEFAULT_SETTING_SOURCES = ["user", "project"]

ProgressSink = Callable[[ProgressEvent], Awaitable[None]]
InitSink = Callable[[dict], None]


def load_mcp_servers(profile: Profile) -> dict[str, Any]:
    """Read the profile's strict, Artemis-only MCP server set."""
    if not profile.mcp_json.exists():
        return {}
    try:
        data = json.loads(profile.mcp_json.read_text())
    except Exception:
        return {}
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


@dataclass
class EngineRunSpec:
    """Everything an engine run needs beyond the Task."""

    can_use_tool: Callable
    hooks: dict
    system_prompt_append: str = ""
    mcp_servers: Optional[dict] = None
    agents: Optional[dict] = None
    setting_sources: Optional[list[str]] = None
    allow_service_files: bool = False


class Engine:
    def __init__(self, profile: Profile, config: Optional[GlobalConfig] = None):
        self.profile = profile
        self.config = config or GlobalConfig.load()
        self.audit = AuditLog(profile.logs_dir)
        self.cost = CostTracker(profile.logs_dir)
        self.log = get_logger("artemis.engine", profile.logs_dir, self.config.log_level)

    # ----- options ---------------------------------------------------------

    def build_system_prompt(self, append_extra: str = "") -> dict:
        parts = []
        if self.profile.soul_md.exists():
            parts.append(self.profile.soul_md.read_text().strip())
        if append_extra.strip():
            parts.append(append_extra.strip())
        append = "\n\n".join(p for p in parts if p)
        return {"type": "preset", "preset": "claude_code", "append": append}

    def build_options(self, task: Task, spec: EngineRunSpec):
        from claude_agent_sdk import ClaudeAgentOptions

        # can_use_tool is the single source of approval truth. SDK allow rules
        # are evaluated BEFORE the callback, so anything listed in allowed_tools
        # would skip gating entirely (even in ask mode). Keep it empty and let
        # the router classify every call. A command's declared tool set is a
        # scoping restriction, applied via `tools` (availability), not approval.
        restrict_tools = task.allowed_tools_override or None
        mcp = spec.mcp_servers if spec.mcp_servers is not None else load_mcp_servers(self.profile)

        return ClaudeAgentOptions(
            model=task.model or self.config.default_model,
            cwd=str(self.profile.workspace),
            env={"CLAUDE_CONFIG_DIR": str(self.profile.claude_home)},
            settings=str(self.profile.settings_json),
            setting_sources=spec.setting_sources or DEFAULT_SETTING_SOURCES,
            mcp_servers=mcp,
            strict_mcp_config=True,
            system_prompt=self.build_system_prompt(spec.system_prompt_append),
            permission_mode="default",
            can_use_tool=spec.can_use_tool,
            hooks=spec.hooks,
            tools=restrict_tools,
            allowed_tools=[],
            agents=spec.agents,
            max_turns=task.max_turns or DEFAULT_MAX_TURNS,
            max_budget_usd=task.max_budget_usd or self.config.budgets.per_task_usd,
            resume=task.resume_session_id,
            include_partial_messages=False,
        )

    # ----- run -------------------------------------------------------------

    async def run(self, task: Task, spec: EngineRunSpec,
                  on_progress: Optional[ProgressSink] = None,
                  on_init: Optional[InitSink] = None) -> RunResult:
        from claude_agent_sdk import (AssistantMessage, ClaudeSDKClient,
                                      ResultMessage, SystemMessage, TextBlock,
                                      ThinkingBlock, ToolResultBlock, ToolUseBlock)

        prompt = task.prompt or ""
        result = RunResult()

        async def emit(ev: ProgressEvent):
            if on_progress:
                try:
                    await on_progress(ev)
                except Exception:
                    self.log.exception("progress sink failed")

        opts = self.build_options(task, spec)
        self.audit.task_event(task.id, "run_start", source=task.source.value,
                              profile=self.profile.name)
        try:
            async with ClaudeSDKClient(options=opts) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage):
                        if msg.subtype == "init":
                            if on_init:
                                on_init(msg.data)
                            self.log.info("init model=%s mcp=%s",
                                          msg.data.get("model"),
                                          msg.data.get("mcp_servers"))
                    elif isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                result.final_text = block.text
                                await emit(ProgressEvent("text", block.text))
                            elif isinstance(block, ThinkingBlock):
                                await emit(ProgressEvent("thinking", block.thinking[:200]))
                            elif isinstance(block, ToolUseBlock):
                                await emit(ProgressEvent(
                                    "tool_use",
                                    _tool_summary(block.name, block.input),
                                    tool_name=block.name))
                            elif isinstance(block, ToolResultBlock):
                                await emit(ProgressEvent("tool_result", ""))
                    elif isinstance(msg, ResultMessage):
                        result.session_id = msg.session_id
                        result.cost_usd = msg.total_cost_usd or 0.0
                        result.num_turns = msg.num_turns
                        result.is_error = msg.is_error
                        result.duration_ms = msg.duration_ms
                        result.permission_denials = msg.permission_denials or []
                        if msg.result:
                            result.final_text = msg.result
                        if msg.errors:
                            result.error = "; ".join(str(e) for e in msg.errors)
        except Exception as exc:  # CLINotFound, connection, etc.
            self.log.exception("engine run failed")
            result.is_error = True
            result.error = f"{type(exc).__name__}: {exc}"

        if result.cost_usd:
            self.cost.add(task.id, result.cost_usd)
        self.audit.task_event(task.id, "run_end", is_error=result.is_error,
                              cost=result.cost_usd, turns=result.num_turns,
                              session=result.session_id)
        return result


    async def quick(self, system: str, prompt: str,
                    model: str = "claude-haiku-4-5-20251001") -> str:
        """One-shot, tool-free helper run in the isolated profile (no approval,
        no streaming). Used for cheap auxiliary calls like memory extraction."""
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      ResultMessage, TextBlock, query)

        opts = ClaudeAgentOptions(
            model=model,
            cwd=str(self.profile.workspace),
            env={"CLAUDE_CONFIG_DIR": str(self.profile.claude_home)},
            settings=str(self.profile.settings_json),
            setting_sources=DEFAULT_SETTING_SOURCES,
            mcp_servers={}, strict_mcp_config=True,
            system_prompt=system, permission_mode="default",
            allowed_tools=[], disallowed_tools=["Bash", "Write", "Edit",
                                                "WebFetch", "WebSearch", "Task", "Agent"],
            max_turns=1,
        )
        out: list[str] = []
        try:
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            out.append(b.text)
                elif isinstance(msg, ResultMessage):
                    if msg.total_cost_usd:
                        self.cost.add("quick", msg.total_cost_usd)
        except Exception:
            self.log.debug("quick() failed", exc_info=True)
        return "".join(out).strip()


def _tool_summary(name: str, tool_input: dict) -> str:
    if name == "Bash":
        return f"$ {str(tool_input.get('command',''))[:80]}"
    for k in ("file_path", "path", "url", "pattern", "query", "notebook_path"):
        if tool_input.get(k):
            return f"{name} {tool_input[k]}"
    return name
