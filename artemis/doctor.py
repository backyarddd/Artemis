"""Health and isolation self-check (Section 8 / Phase 0 acceptance).

doctor proves the locked isolation guarantee: a real task loads only Artemis's
skills/agents/MCP and the operator's personal Claude config is provably absent.
It also verifies the installed SDK exposes the option names the engine relies on,
and reports auth and budget status.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .approval import ApprovalRouter
from .config import GlobalConfig
from .engine import Engine, EngineRunSpec, load_mcp_servers
from .hooks import build_hooks
from .models import ApprovalMode, Task, TaskSource
from .profiles import Profile, ProfileManager, operator_logged_in

EXPECTED_OPTIONS = [
    "model", "cwd", "env", "settings", "setting_sources", "mcp_servers",
    "strict_mcp_config", "system_prompt", "permission_mode", "can_use_tool",
    "hooks", "allowed_tools", "disallowed_tools", "agents", "max_turns",
    "max_budget_usd", "resume",
]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    init_data: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))


def verify_sdk_options() -> Check:
    import dataclasses
    from claude_agent_sdk import ClaudeAgentOptions

    have = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    missing = [o for o in EXPECTED_OPTIONS if o not in have]
    if missing:
        return Check("sdk_options", False, f"missing options: {missing}")
    from claude_agent_sdk import __version__
    return Check("sdk_options", True, f"claude-agent-sdk {__version__} ok")


def operator_custom_names() -> set[str]:
    """Names of the operator's personal skills/agents/commands (read-only).

    These must never appear in an isolated Artemis run. Built-in CLI skills are
    excluded by construction since they do not live under ~/.claude.
    """
    home = Path.home() / ".claude"
    names: set[str] = set()
    for sub in ("skills", "agents", "commands"):
        d = home / sub
        if d.is_dir():
            for entry in d.iterdir():
                if entry.name.startswith("."):
                    continue
                names.add(entry.stem if entry.is_file() else entry.name)
    return names


async def run_isolation_check(profile: Profile, config: GlobalConfig) -> tuple[Check, dict]:
    """Run a trivial isolated task and assert no operator config leaked in."""
    engine = Engine(profile, config)
    router = ApprovalRouter(profile, engine.audit)
    spec = EngineRunSpec(
        can_use_tool=router.make_callback("doctor", None, ApprovalMode.BYPASS),
        hooks=build_hooks(profile, on_block=lambda *a: None),
    )
    task = Task(prompt="Reply with exactly the word: PONG. Use no tools.",
                profile=profile.name, source=TaskSource.CLI,
                approval_mode_override=ApprovalMode.BYPASS,
                max_turns=1, model="claude-haiku-4-5-20251001")

    captured: dict = {}

    def on_init(data: dict):
        captured.update(data)

    result = await engine.run(task, spec, on_init=on_init)

    if not captured:
        return Check("isolation", False,
                     f"no init received (run error: {result.error})"), captured

    operator = operator_custom_names()
    loaded_skills = set(captured.get("skills", []))
    loaded_agents = set(captured.get("agents", []))
    loaded_cmds = set(captured.get("slash_commands", []))
    loaded_mcp = captured.get("mcp_servers", [])
    mcp_names = {m.get("name") if isinstance(m, dict) else m for m in loaded_mcp}

    leaked = (loaded_skills | loaded_agents | loaded_cmds) & operator
    artemis_mcp = set(load_mcp_servers(profile).keys())
    mcp_leak = {m for m in mcp_names if m and m not in artemis_mcp}

    if leaked:
        return Check("isolation", False,
                     f"operator skills/agents/commands leaked: {sorted(leaked)[:10]}"), captured
    if mcp_leak:
        return Check("isolation", False, f"foreign MCP servers loaded: {mcp_leak}"), captured
    if result.is_error:
        return Check("isolation", False,
                     f"isolation run errored: {result.error}"), captured

    detail = (f"clean: {len(operator)} operator items checked, none leaked; "
              f"model={captured.get('model')} mcp={sorted(mcp_names)} "
              f"apiKeySource={captured.get('apiKeySource')}")
    return Check("isolation", True, detail), captured


async def doctor(profile_name: Optional[str] = None, run_isolation: bool = True) -> Report:
    config = GlobalConfig.load()
    report = Report()

    report.checks.append(verify_sdk_options())

    pm = ProfileManager()
    name = profile_name or pm.active_name()
    try:
        profile = pm.get(name)
        report.add("profile", True, f"profile '{name}' at {profile.root}")
    except FileNotFoundError:
        report.add("profile", False, f"profile '{name}' not found; run 'artemis setup'")
        return report

    # Auth.
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        report.add("auth", True, "ANTHROPIC_API_KEY set")
    elif profile.credentials_file.exists():
        report.add("auth", True, "subscription bridged into profile claude-home")
    elif operator_logged_in():
        report.add("auth", False, "operator logged in but not bridged; run 'artemis setup'")
    else:
        report.add("auth", False, "not authenticated; run 'claude login' or set ANTHROPIC_API_KEY")

    # Workspace + soul.
    report.add("workspace", profile.workspace.is_dir(),
               f"cwd={profile.workspace}")
    report.add("soul", profile.soul_md.exists(), f"persona at {profile.soul_md}")

    if run_isolation and report.ok:
        check, init = await run_isolation_check(profile, config)
        report.checks.append(check)
        report.init_data = init

    return report


def format_report(report: Report) -> str:
    lines = []
    for c in report.checks:
        mark = "PASS" if c.ok else "FAIL"
        lines.append(f"[{mark}] {c.name}: {c.detail}")
    lines.append(f"\noverall: {'PASS' if report.ok else 'FAIL'}")
    return "\n".join(lines)
