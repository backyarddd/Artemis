"""Artemis CLI (typer). Subcommands: setup, run, daemon, profile, channel,
mcp, skill, cmd, cron, mode, memory, doctor.

State-mutating commands operate on the active profile directly. Changes to cron,
mcp, and commands take effect on the next daemon start (the running daemon reads
these from disk at startup).
"""

from __future__ import annotations

from typing import Optional

import anyio
import typer

import secrets as _secrets

from .config import (GlobalConfig, ensure_api_key_env, get_secret, set_secret)
from .models import ApprovalMode, ChannelTarget, Task, TaskSource
from .profiles import ProfileManager

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Artemis: host-agnostic always-on autonomous agent.")


def _active_profile():
    pm = ProfileManager()
    try:
        return pm.get(pm.active_name())
    except FileNotFoundError:
        typer.secho("No Artemis profile found. Run 'artemis setup' first.",
                    fg=typer.colors.RED)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Top-level commands.
# ---------------------------------------------------------------------------

@app.command()
def setup(non_interactive: bool = typer.Option(False, "--non-interactive", "-n"),
          answers: Optional[str] = typer.Option(None, help="Path to a YAML/JSON answers file")):
    """Guided first-run wizard (re-runnable). Use -n for headless provisioning."""
    from .setup import run_wizard
    anyio.run(run_wizard, non_interactive, answers)


@app.command()
def run(prompt: str = typer.Argument(..., help="The task prompt"),
        profile: Optional[str] = typer.Option(None),
        mode: Optional[str] = typer.Option(None, help="bypass|auto|ask")):
    """Run a single task in the foreground and print the result."""
    anyio.run(_run_once, prompt, profile, mode)


@app.command()
def doctor(profile: Optional[str] = typer.Option(None),
           no_isolation: bool = typer.Option(False, "--no-isolation")):
    """Health and isolation self-check."""
    from .doctor import doctor as run_doctor, format_report
    report = anyio.run(run_doctor, profile, not no_isolation)
    typer.echo(format_report(report))
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def mode(value: Optional[str] = typer.Argument(None, help="bypass|auto|ask")):
    """Show or set the active profile's default approval mode."""
    from .approval import ProfileState
    prof = _active_profile()
    state = ProfileState.load(prof)
    if value is None:
        typer.echo(f"approval mode: {state.approval_mode.value}")
        return
    m = ApprovalMode.coerce(value, None)
    if m is None:
        typer.secho("usage: artemis mode [bypass|auto|ask]", fg=typer.colors.RED)
        raise typer.Exit(1)
    state.approval_mode = m
    state.save()
    typer.echo(f"approval mode set to {m.value}")


@app.command()
def serve(host: Optional[str] = typer.Option(None, help="bind host (default 127.0.0.1)"),
          port: Optional[int] = typer.Option(None, help="bind port (default 8799)"),
          profile: Optional[str] = typer.Option(None),
          no_auth: bool = typer.Option(False, "--no-auth", help="disable Bearer key check"),
          rotate_key: bool = typer.Option(False, "--rotate-key", help="generate a fresh API key")):
    """Run the OpenAI-compatible HTTP endpoint in the foreground.

    Point any OpenAI client at the printed base_url. Other programs then use your
    Claude subscription as a drop-in OpenAI backend.
    """
    import uvicorn
    from .server.openai_api import OpenAIServer

    pm = ProfileManager()
    name = profile or pm.active_name()
    try:
        prof = pm.get(name)
    except FileNotFoundError:
        typer.secho("No profile. Run 'artemis setup'.", fg=typer.colors.RED)
        raise typer.Exit(1)
    config = GlobalConfig.load()
    if host:
        config.openai_server.host = host
    if port:
        config.openai_server.port = port
    if no_auth:
        config.openai_server.require_auth = False
    ensure_api_key_env(name)
    pm.bridge_auth(prof)

    key = None
    if config.openai_server.require_auth:
        key = get_secret("openai_api_key", name)
        if rotate_key or not key:
            key = "sk-artemis-" + _secrets.token_urlsafe(24)
            set_secret("openai_api_key", key, name)
    h, p = config.openai_server.host, config.openai_server.port
    base = f"http://{h}:{p}/v1"
    typer.secho(f"Artemis OpenAI endpoint: {base}", fg=typer.colors.GREEN)
    typer.echo(f"  API key: {key if key else '(auth disabled)'}")
    typer.echo(f"  Example: curl {base}/chat/completions -H 'Authorization: Bearer "
               f"{key or 'EMPTY'}' -H 'Content-Type: application/json' "
               f"-d '{{\"model\":\"{config.default_model}\",\"messages\":"
               f"[{{\"role\":\"user\",\"content\":\"hi\"}}]}}'")
    server = OpenAIServer(prof, config)
    uvicorn.run(server.app, host=h, port=p, log_level="warning")


async def _run_once(prompt: str, profile: Optional[str], mode: Optional[str]) -> None:
    from .channels.cli import CLIChannel
    from .channels.gateway import Gateway
    from .orchestrator import Orchestrator

    pm = ProfileManager()
    name = profile or pm.active_name()
    try:
        pm.get(name)
    except FileNotFoundError:
        typer.secho("No profile. Run 'artemis setup'.", fg=typer.colors.RED)
        raise typer.Exit(1)
    config = GlobalConfig.load()
    ensure_api_key_env(name)
    pm.bridge_auth(pm.get(name))

    gateway = Gateway(config, pm.get(name).logs_dir)
    gateway.register(CLIChannel(lambda m: None, {}))
    orch = Orchestrator(config, gateway, name, pm)
    await gateway.start()
    task = Task(prompt=prompt, source=TaskSource.CLI,
                channel_target=ChannelTarget("cli", "local"), profile=name,
                approval_mode_override=ApprovalMode.coerce(mode, None) if mode else None)
    await orch.run_task(task)
    await gateway.stop()


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------

daemon_app = typer.Typer(no_args_is_help=True, help="Run and manage the daemon.")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("run")
def daemon_run(profile: Optional[str] = typer.Option(None),
               foreground: bool = typer.Option(False, "--foreground", "-f")):
    """Run the supervised daemon (used by service units)."""
    from .daemon import run_daemon
    anyio.run(run_daemon, profile, foreground)


@daemon_app.command("install")
def daemon_install(profile: Optional[str] = typer.Option(None),
                   method: Optional[str] = typer.Option(None, help="launchd|systemd|docker")):
    from .service import install as svc
    typer.echo(svc.install(method, profile))


@daemon_app.command("status")
def daemon_status(method: Optional[str] = typer.Option(None)):
    from .service import install as svc
    typer.echo(svc.status(method))


@daemon_app.command("stop")
def daemon_stop(method: Optional[str] = typer.Option(None)):
    from .service import install as svc
    typer.echo(svc.stop(method))


@daemon_app.command("restart")
def daemon_restart(method: Optional[str] = typer.Option(None)):
    from .service import install as svc
    typer.echo(svc.restart(method))


@daemon_app.command("uninstall")
def daemon_uninstall(method: Optional[str] = typer.Option(None)):
    from .service import install as svc
    typer.echo(svc.uninstall(method))


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

profile_app = typer.Typer(no_args_is_help=True, help="Manage personas/profiles.")
app.add_typer(profile_app, name="profile")


@profile_app.command("create")
def profile_create(name: str, persona: Optional[str] = typer.Option(None)):
    pm = ProfileManager()
    prof = pm.create(name, persona)
    pm.bridge_auth(prof)
    typer.echo(f"created profile '{name}' at {prof.root}")


@profile_app.command("list")
def profile_list():
    pm = ProfileManager()
    active = pm.active_name()
    for n in pm.list():
        typer.echo(f"{'* ' if n == active else '  '}{n}")


@profile_app.command("switch")
def profile_switch(name: str):
    pm = ProfileManager()
    try:
        pm.get(name)
    except FileNotFoundError:
        typer.secho(f"no such profile '{name}'", fg=typer.colors.RED)
        raise typer.Exit(1)
    config = GlobalConfig.load()
    config.default_profile = name
    config.save()
    typer.echo(f"default profile set to '{name}' (or set ARTEMIS_PROFILE)")


# ---------------------------------------------------------------------------
# channel
# ---------------------------------------------------------------------------

channel_app = typer.Typer(no_args_is_help=True, help="Add/verify channels.")
app.add_typer(channel_app, name="channel")


@channel_app.command("add")
def channel_add(channel_type: str = typer.Argument(..., help="telegram|discord")):
    """Sub-wizard to configure a channel."""
    from .setup import channel_subwizard
    anyio.run(channel_subwizard, channel_type)


@channel_app.command("list")
def channel_list():
    from .config import get_secret
    prof = _active_profile()
    for ch in ("telegram", "discord"):
        configured = bool(get_secret(f"{ch}_token", prof.name))
        typer.echo(f"  {ch}: {'configured' if configured else 'not configured'}")
    typer.echo("  cli: always available")


# ---------------------------------------------------------------------------
# mcp / skill / cmd / cron / memory
# ---------------------------------------------------------------------------

mcp_app = typer.Typer(no_args_is_help=True, help="Per-profile MCP servers.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("list")
def mcp_list():
    from .registry.mcp import McpRegistry
    servers = McpRegistry(_active_profile()).list()
    typer.echo("\n".join(f"  {k}" for k in servers) or "  (none)")


@mcp_app.command("add")
def mcp_add(name: str, command: Optional[str] = typer.Option(None),
            url: Optional[str] = typer.Option(None),
            arg: list[str] = typer.Option([], "--arg")):
    from .registry.mcp import McpRegistry
    cfg = {"command": command, "args": arg} if command else {"type": "http", "url": url}
    McpRegistry(_active_profile()).add(name, cfg)
    typer.echo(f"added MCP server '{name}'")


@mcp_app.command("remove")
def mcp_remove(name: str):
    from .registry.mcp import McpRegistry
    McpRegistry(_active_profile()).remove(name)
    typer.echo(f"removed '{name}'")


@mcp_app.command("test")
def mcp_test(name: str):
    from .registry.mcp import McpRegistry
    ok, detail = anyio.run(McpRegistry(_active_profile()).test, name)
    typer.echo(f"{'OK' if ok else 'FAIL'}: {detail}")


skill_app = typer.Typer(no_args_is_help=True, help="Per-profile skills.")
app.add_typer(skill_app, name="skill")


@skill_app.command("list")
def skill_list():
    from .registry.skills import SkillRegistry
    for s in SkillRegistry(_active_profile()).list():
        typer.echo(f"  {s['name']} ({s.get('scope','')})")


@skill_app.command("add")
def skill_add(name: str, path: Optional[str] = typer.Option(None, help="source skill dir")):
    from .registry.skills import SkillRegistry
    SkillRegistry(_active_profile()).add(name, source_path=path)
    typer.echo(f"added skill '{name}'")


@skill_app.command("remove")
def skill_remove(name: str):
    from .registry.skills import SkillRegistry
    SkillRegistry(_active_profile()).remove(name)
    typer.echo(f"removed skill '{name}'")


cmd_app = typer.Typer(no_args_is_help=True, help="Saved command templates.")
app.add_typer(cmd_app, name="cmd")


@cmd_app.command("list")
def cmd_list():
    from .registry.commands import CommandRegistry
    for c in CommandRegistry(_active_profile()).list():
        flag = " (pending)" if c.pending else ""
        typer.echo(f"  {c.name}: {c.description}{flag}")


@cmd_app.command("new")
def cmd_new(name: str, description: str = typer.Option("", "--desc"),
            body: str = typer.Option("Describe the task here. Use {arg} placeholders.")):
    from .registry.commands import CommandRegistry
    CommandRegistry(_active_profile()).create(name, description, body)
    typer.echo(f"created command '{name}'")


@cmd_app.command("run")
def cmd_run(name: str, args: list[str] = typer.Argument(None)):
    kv = {}
    for tok in (args or []):
        if "=" in tok:
            k, v = tok.split("=", 1)
            kv[k] = v
    anyio.run(_run_command, name, kv)


@cmd_app.command("remove")
def cmd_remove(name: str):
    from .registry.commands import CommandRegistry
    CommandRegistry(_active_profile()).remove(name)
    typer.echo(f"removed '{name}'")


@cmd_app.command("approve")
def cmd_approve(name: str):
    from .registry.commands import CommandRegistry
    CommandRegistry(_active_profile()).approve(name)
    typer.echo(f"approved agent-authored command '{name}'")


async def _run_command(name: str, kv: dict) -> None:
    from .channels.cli import CLIChannel
    from .channels.gateway import Gateway
    from .orchestrator import Orchestrator
    pm = ProfileManager()
    name_p = pm.active_name()
    config = GlobalConfig.load()
    ensure_api_key_env(name_p)
    pm.bridge_auth(pm.get(name_p))
    gateway = Gateway(config, pm.get(name_p).logs_dir)
    gateway.register(CLIChannel(lambda m: None, {}))
    orch = Orchestrator(config, gateway, name_p, pm)
    await gateway.start()
    task = Task(command_ref=name, command_args=kv, source=TaskSource.CLI,
                channel_target=ChannelTarget("cli", "local"), profile=name_p)
    await orch.run_task(task)
    await gateway.stop()


cron_app = typer.Typer(no_args_is_help=True, help="Scheduled tasks.")
app.add_typer(cron_app, name="cron")


@cron_app.command("add")
def cron_add(schedule: str = typer.Argument(..., help="5-field cron expr"),
             prompt: Optional[str] = typer.Option(None),
             command: Optional[str] = typer.Option(None, help="command_ref")):
    from .triggers.scheduler import Scheduler
    prof = _active_profile()
    sch = Scheduler(prof, GlobalConfig.load(), lambda t: None)
    jid = sch.add_job(schedule, prompt=prompt, command_ref=command)
    typer.echo(f"added cron job {jid} (takes effect on next daemon start)")


@cron_app.command("list")
def cron_list():
    from .triggers.scheduler import Scheduler
    sch = Scheduler(_active_profile(), GlobalConfig.load(), lambda t: None)
    for j in sch.list_jobs():
        typer.echo(f"  {j.get('id')}: {j.get('cron')} -> {j.get('prompt') or j.get('command_ref')}")


@cron_app.command("remove")
def cron_remove(job_id: str):
    from .triggers.scheduler import Scheduler
    sch = Scheduler(_active_profile(), GlobalConfig.load(), lambda t: None)
    ok = sch.remove_job(job_id)
    typer.echo("removed" if ok else "not found")


memory_app = typer.Typer(no_args_is_help=True, help="Inspect memory.")
app.add_typer(memory_app, name="memory")


@memory_app.command("search")
def memory_search(query: str, limit: int = typer.Option(5)):
    from .memory.store import MemoryStore
    prof = _active_profile()
    store = MemoryStore(prof.db_path)
    for f in store.search(query, limit=limit, profile=prof.name):
        typer.echo(f"  - {f.get('text')}")


@memory_app.command("recent")
def memory_recent(limit: int = typer.Option(10)):
    from .memory.store import MemoryStore
    prof = _active_profile()
    for f in MemoryStore(prof.db_path).recent(limit=limit, profile=prof.name):
        typer.echo(f"  - {f.get('text')}")


@memory_app.command("add")
def memory_add(text: str):
    from .memory.store import MemoryStore
    prof = _active_profile()
    MemoryStore(prof.db_path).add_fact(text, profile=prof.name)
    typer.echo("stored")


if __name__ == "__main__":
    app()
