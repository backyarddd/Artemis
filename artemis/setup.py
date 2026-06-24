"""Guided first-run wizard plus granular sub-wizards and a non-interactive mode.

`artemis setup` takes a fresh install to a working, supervised agent with no
manual file editing. Re-runnable to reconfigure. The non-interactive path reads
an answers file (YAML/JSON) and/or env secrets so headless VPS and Docker can
provision unattended.
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import Any, Optional

from .config import (GlobalConfig, get_secret, set_secret)
from .profiles import ProfileManager, operator_logged_in

try:
    from rich.console import Console
    _c = Console()
    def out(t: str = "") -> None: _c.print(t)
except Exception:
    def out(t: str = "") -> None: print(t)

CHEAT = """
Artemis is ready. Quick reference:
  artemis run "do something"        run a one-off task
  artemis gateway run -f            run the gateway in the foreground
  artemis gateway install           install + start the OS service
  artemis doctor                    health + isolation check
  artemis mode auto|ask|bypass      set approval posture
  artemis cmd list | cmd run NAME   saved commands
  artemis cron add "*/30 * * * *" --prompt "..."
  artemis channel add telegram      add another channel
From any channel: /help /mode /status /pause /cmd /mcp /skills /profile
"""


def _load_answers(path: Optional[str]) -> dict:
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.exists():
        out(f"answers file not found: {p}")
        return {}
    text = p.read_text()
    try:
        if p.suffix in (".yaml", ".yml"):
            import yaml
            return yaml.safe_load(text) or {}
        return json.loads(text)
    except Exception as exc:
        out(f"could not parse answers file: {exc}")
        return {}


class Wizard:
    def __init__(self, interactive: bool, answers: dict):
        self.interactive = interactive
        self.answers = answers or {}

    # ----- prompt helpers (respect non-interactive) -----------------------

    def ask(self, key: str, prompt: str, default: Optional[str] = None,
            choices: Optional[list] = None) -> Optional[str]:
        if key in self.answers and self.answers[key] is not None:
            return str(self.answers[key])
        if not self.interactive:
            return default
        import typer
        return typer.prompt(prompt, default=default if default is not None else "")

    def confirm(self, key: str, prompt: str, default: bool = False) -> bool:
        if key in self.answers and self.answers[key] is not None:
            return bool(self.answers[key])
        if not self.interactive:
            return default
        import typer
        return typer.confirm(prompt, default=default)

    def secret(self, key: str, prompt: str) -> Optional[str]:
        if key in self.answers and self.answers[key]:
            return str(self.answers[key])
        env = get_secret(key)
        if env:
            return env
        if not self.interactive:
            return None
        import typer
        val = typer.prompt(prompt, hide_input=True, default="")
        return val or None

    # ----- the flow --------------------------------------------------------

    async def run(self) -> None:
        out("[bold]Artemis setup[/bold]" if _has_rich() else "Artemis setup")
        self._step_env()
        profile = self._step_profile()
        self._step_auth(profile)
        self._step_approval()
        self._step_channels(profile)
        self._step_mcp(profile)
        self._step_skills_commands(profile)
        self._step_goals_schedule(profile)
        self._step_budgets()
        self._step_openai_server(profile)
        self._step_service(profile)
        await self._step_verify(profile)
        out(CHEAT)

    def _step_env(self) -> None:
        out(f"OS: {platform.system()}  Python: {sys.version.split()[0]}")
        out(f"claude login: {'yes' if operator_logged_in() else 'no'}")
        existing = ProfileManager().list()
        if existing:
            out(f"existing profiles: {', '.join(existing)}")

    def _step_profile(self):
        pm = ProfileManager()
        name = self.ask("profile", "Profile name", default="default") or "default"
        persona = self.ask("persona", "Persona name", default="Artemis") or "Artemis"
        prof = pm.create(name, persona)
        out(f"profile '{name}' at {prof.root}")
        cfg = GlobalConfig.load()
        cfg.default_profile = name
        cfg.save()
        return prof

    def _step_auth(self, profile) -> None:
        pm = ProfileManager()
        choice = self.ask("auth", "Auth: subscription or api_key",
                          default="subscription" if operator_logged_in() else "api_key",
                          choices=["subscription", "api_key"])
        if choice == "api_key":
            key = self.secret("anthropic_api_key", "Anthropic API key")
            if key:
                set_secret("anthropic_api_key", key)
                out("stored API key in keyring")
        status = pm.bridge_auth(profile)
        out(f"auth bridge: {status}")

    def _step_approval(self) -> None:
        m = self.ask("approval_mode",
                     "Default approval mode (bypass=auto-approve, auto=gate risky, ask=gate all)",
                     default="auto", choices=["bypass", "auto", "ask"]) or "auto"
        cfg = GlobalConfig.load()
        cfg.default_approval_mode = m
        cfg.save()
        out(f"default approval mode: {m}")

    def _step_channels(self, profile) -> None:
        cfg = GlobalConfig.load()
        chans = self.answers.get("channels", {})
        if self.interactive and not chans:
            if self.confirm("_tg", "Configure Telegram?", default=False):
                self._configure_telegram(profile, cfg, {})
            if self.confirm("_dc", "Configure Discord?", default=False):
                self._configure_discord(profile, cfg, {})
        else:
            if "telegram" in chans:
                self._configure_telegram(profile, cfg, chans["telegram"])
            if "discord" in chans:
                self._configure_discord(profile, cfg, chans["discord"])
        cfg.save()
        out("CLI channel is always available.")

    def _configure_telegram(self, profile, cfg, spec: dict) -> None:
        if self.interactive and not spec:
            out("Open Telegram, message @BotFather, send /newbot, copy the token.")
            token = self.secret("telegram_token", "Telegram bot token")
            uid = self.ask("telegram_user_id",
                           "Your Telegram numeric user id (message @userinfobot)")
            spec = {"token": token, "allow": [uid] if uid else [],
                    "default_conversation": uid}
        token = spec.get("token") or get_secret("telegram_token", profile.name)
        if token:
            set_secret("telegram_token", token, profile.name)
        allow = [str(x) for x in (spec.get("allow") or []) if x]
        if allow:
            cfg.channel_allowlists["telegram"] = allow
        if spec.get("default_conversation"):
            cfg.channel_defaults["telegram"] = str(spec["default_conversation"])
        out(f"telegram configured (allow: {allow or 'none'})")

    def _configure_discord(self, profile, cfg, spec: dict) -> None:
        if self.interactive and not spec:
            out("Create a bot at discord.com/developers, enable Message Content intent, copy the token.")
            token = self.secret("discord_token", "Discord bot token")
            uid = self.ask("discord_user_id", "Allowed Discord user id")
            chan = self.ask("discord_channel_id", "Default Discord channel id")
            spec = {"token": token, "allow": [uid] if uid else [],
                    "default_conversation": chan}
        token = spec.get("token") or get_secret("discord_token", profile.name)
        if token:
            set_secret("discord_token", token, profile.name)
        allow = [str(x) for x in (spec.get("allow") or []) if x]
        if allow:
            cfg.channel_allowlists["discord"] = allow
        if spec.get("default_conversation"):
            cfg.channel_defaults["discord"] = str(spec["default_conversation"])
        out(f"discord configured (allow: {allow or 'none'})")

    def _step_mcp(self, profile) -> None:
        from .registry.mcp import McpRegistry
        servers = self.answers.get("mcp", {})
        if not servers:
            return
        reg = McpRegistry(profile)
        for name, cfg in servers.items():
            try:
                reg.add(name, cfg)
                out(f"added MCP server '{name}'")
            except Exception as exc:
                out(f"MCP '{name}' failed: {exc}")

    def _step_skills_commands(self, profile) -> None:
        from .registry.commands import seed_starter_pack
        seed_starter_pack(profile)
        out("seeded starter command pack (research, ship, standup, track, digest)")
        imp = self.answers.get("import_commands")
        if imp:
            out(f"(import from {imp} not auto-run; copy files into {profile.commands_dir})")

    def _step_goals_schedule(self, profile) -> None:
        goals = self.answers.get("goals")
        if goals:
            text = profile.goals_md.read_text() if profile.goals_md.exists() else ""
            text += "\n" + "\n".join(f"- {g}" for g in goals) + "\n"
            profile.goals_md.write_text(text)
            out(f"wrote {len(goals)} standing goal(s)")
        crons = self.answers.get("crons", [])
        if crons:
            from .triggers.scheduler import Scheduler
            sch = Scheduler(profile, GlobalConfig.load(), lambda t: None)
            for c in crons:
                try:
                    sch.add_job(c["schedule"], prompt=c.get("prompt"),
                                command_ref=c.get("command_ref"))
                    out(f"added cron {c['schedule']}")
                except Exception as exc:
                    out(f"cron failed: {exc}")

    def _step_budgets(self) -> None:
        cfg = GlobalConfig.load()
        b = self.answers.get("budgets", {})
        per = b.get("per_task_usd")
        daily = b.get("daily_usd")
        if self.interactive and not b:
            per = self.ask("per_task_usd", "Per-task USD cap", default=str(cfg.budgets.per_task_usd))
            daily = self.ask("daily_usd", "Daily USD cap", default=str(cfg.budgets.daily_usd))
        try:
            if per is not None:
                cfg.budgets.per_task_usd = float(per)
            if daily is not None:
                cfg.budgets.daily_usd = float(daily)
        except (TypeError, ValueError):
            out("invalid budget value; keeping defaults")
        cfg.save()
        out(f"budgets: ${cfg.budgets.per_task_usd}/task, ${cfg.budgets.daily_usd}/day")

    def _step_openai_server(self, profile) -> None:
        import secrets as _secrets
        cfg = GlobalConfig.load()
        spec = self.answers.get("openai_server")
        if spec is None:
            if not (self.interactive and self.confirm(
                    "_oai", "Expose an OpenAI-compatible HTTP endpoint for other apps?",
                    default=False)):
                return
            spec = {"enabled": True}
            port = self.ask("openai_port", "Port", default=str(cfg.openai_server.port))
            spec["port"] = int(port) if port else cfg.openai_server.port
        if not spec.get("enabled"):
            return
        cfg.openai_server.enabled = True
        cfg.openai_server.host = spec.get("host", cfg.openai_server.host)
        cfg.openai_server.port = int(spec.get("port", cfg.openai_server.port))
        cfg.openai_server.require_auth = spec.get("require_auth", True)
        cfg.save()
        key = None
        if cfg.openai_server.require_auth:
            key = spec.get("api_key") or get_secret("openai_api_key", profile.name)
            if not key:
                key = "sk-artemis-" + _secrets.token_urlsafe(24)
            set_secret("openai_api_key", key, profile.name)
        base = f"http://{cfg.openai_server.host}:{cfg.openai_server.port}/v1"
        out(f"OpenAI endpoint enabled at {base}")
        out(f"  API key: {key if key else '(auth disabled)'}")
        out("  Starts with the gateway, or run 'artemis serve' now.")

    def _step_service(self, profile) -> None:
        from .service import install as svc
        method = self.ask("service", "Service install (launchd/systemd/docker/none)",
                          default="none")
        if method and method != "none":
            try:
                out(svc.install(method, profile.name))
            except Exception as exc:
                out(f"service install failed: {exc}")
        else:
            out("skipping service install; run 'artemis gateway run -f' for foreground.")

    async def _step_verify(self, profile) -> None:
        from .doctor import doctor, format_report
        out("\nrunning doctor (isolation proof)...")
        report = await doctor(profile.name, run_isolation=True)
        out(format_report(report))
        # Channel test messages.
        cfg = GlobalConfig.load()
        for ch in ("telegram", "discord"):
            conv = cfg.channel_defaults.get(ch)
            tok = get_secret(f"{ch}_token", profile.name)
            if conv and tok:
                ok = await _channel_test(ch, tok, conv)
                out(f"{ch} test message: {'sent' if ok else 'failed'}")


def _has_rich() -> bool:
    try:
        import rich  # noqa
        return True
    except Exception:
        return False


async def _channel_test(channel: str, token: str, conversation_id: str) -> bool:
    try:
        if channel == "telegram":
            from .channels.telegram import TelegramAdapter
            adapter = TelegramAdapter(lambda m: None, {"token": token})
        else:
            from .channels.discord import DiscordAdapter
            adapter = DiscordAdapter(lambda m: None, {"token": token})
        await adapter.start()
        ok = await adapter.send_test_message(conversation_id)
        await adapter.stop()
        return ok
    except Exception:
        return False


async def run_wizard(non_interactive: bool = False, answers_path: Optional[str] = None) -> None:
    answers = _load_answers(answers_path)
    wiz = Wizard(interactive=not non_interactive, answers=answers)
    await wiz.run()


async def channel_subwizard(channel_type: str) -> None:
    """Granular `artemis channel add <type>` sub-wizard."""
    pm = ProfileManager()
    profile = pm.get(pm.active_name())
    cfg = GlobalConfig.load()
    wiz = Wizard(interactive=True, answers={})
    if channel_type == "telegram":
        wiz._configure_telegram(profile, cfg, {})
    elif channel_type == "discord":
        wiz._configure_discord(profile, cfg, {})
    else:
        out(f"unknown channel type: {channel_type}")
        return
    cfg.save()
    out("channel saved; restart the gateway to pick it up.")
