"""Orchestrator: the task runner and the channel control plane.

run_task is the queue's Runner. For each task it resolves a command template,
injects recalled memory, wires the approval router and catastrophe guards,
streams progress to the bound channel, persists the session for resume, and
extracts durable facts afterward. handle_message parses inbound channel traffic
into control actions (/mode, /status, /pause, /cmd, ...) or freeform tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .approval import ApprovalRouter
from .config import GlobalConfig
from .engine import Engine, EngineRunSpec
from .hooks import build_hooks
from .models import (ApprovalMode, ChannelTarget, InboundMessage, RunResult,
                     Task, TaskSource)
from .observability import get_logger
from .profiles import Profile, ProfileManager

CHEAT_SHEET = """Artemis commands:
/help                show this
/mode [bypass|auto|ask]   show or set approval mode
/status              queue, mode, and cost
/pause  /resume      pause or resume the autonomous goal loop
/cmd <name> k=v ...  run a saved command
/cmd list            list saved commands
/mcp                 list MCP servers
/skills              list skills
/profile [list|switch <name>]
anything else        run as a one-off task"""


@dataclass
class ProfileContext:
    profile: Profile
    engine: Engine
    router: ApprovalRouter
    store: object  # MemoryStore
    commands: object
    mcp: object
    skills: object


class Orchestrator:
    def __init__(self, config: GlobalConfig, gateway, active_profile: str = "default",
                 profile_manager: Optional[ProfileManager] = None):
        self.config = config
        self.gateway = gateway
        self.active_profile = active_profile
        self.pm = profile_manager or ProfileManager()
        self._ctx: dict[str, ProfileContext] = {}
        self._conv_profile: dict[str, str] = {}
        # Injected by the daemon.
        self.enqueue: Optional[Callable[[Task], Awaitable[str]]] = None
        self.queue = None
        self.scheduler = None
        self.log = get_logger("artemis.orchestrator",
                              self.pm.path_for(active_profile).logs_dir,
                              config.log_level)

    # ----- per-profile components -----------------------------------------

    def context(self, profile_name: str) -> ProfileContext:
        if profile_name in self._ctx:
            return self._ctx[profile_name]
        from .memory.store import MemoryStore
        from .registry.commands import CommandRegistry
        from .registry.mcp import McpRegistry
        from .registry.skills import SkillRegistry

        profile = self.pm.get(profile_name)
        engine = Engine(profile, self.config)
        router = ApprovalRouter(
            profile, engine.audit,
            deliver=self.gateway.deliver_approval if self.gateway else None,
            timeout_seconds=self.config.approval.timeout_seconds,
            default_action=self.config.approval.default_action)
        ctx = ProfileContext(
            profile=profile, engine=engine, router=router,
            store=MemoryStore(profile.db_path),
            commands=CommandRegistry(profile),
            mcp=McpRegistry(profile),
            skills=SkillRegistry(profile))
        self._ctx[profile_name] = ctx
        return ctx

    # ----- the runner ------------------------------------------------------

    async def run_task(self, task: Task) -> RunResult:
        from .memory.extract import extract_and_store
        from .memory.recall import build_query_from_task, recall

        ctx = self.context(task.profile)
        engine, router, store = ctx.engine, ctx.router, ctx.store

        # Daily budget guard.
        if engine.cost.today_total() >= self.config.budgets.daily_usd:
            res = RunResult(is_error=True, error="daily budget exhausted")
            await self._deliver_result(task, res, "Daily budget exhausted; task skipped.")
            return res

        mode_override = task.approval_mode_override

        # Resolve a saved command into a concrete prompt.
        if task.command_ref:
            try:
                resolved = ctx.commands.resolve(task.command_ref, task.command_args)
                task.prompt = resolved["prompt"]
                if resolved.get("allowed_tools"):
                    task.allowed_tools_override = resolved["allowed_tools"]
                if resolved.get("approval_mode") and not mode_override:
                    mode_override = resolved["approval_mode"]
            except Exception as exc:
                res = RunResult(is_error=True, error=f"command error: {exc}")
                await self._deliver_result(task, res, f"Command failed: {exc}")
                return res

        # Memory recall -> system prompt.
        try:
            recalled = recall(store, build_query_from_task(task), profile=task.profile)
        except Exception:
            recalled = ""

        # Conversational resume per channel.
        target = task.channel_target
        if not task.resume_session_id and target is not None:
            try:
                prior = store.get_session(project=target.key(), profile=task.profile)
                if prior and prior.get("session_id"):
                    task.resume_session_id = prior["session_id"]
            except Exception:
                pass

        def on_block(tool_name, reason, tool_input):
            engine.audit.hook_block(task.id, tool_name, reason, tool_input)

        spec = EngineRunSpec(
            can_use_tool=router.make_callback(task.id, target, mode_override),
            hooks=build_hooks(ctx.profile, on_block=on_block,
                              extra_allowed=router.state.extra_allowed_paths),
            system_prompt_append=recalled,
            agents=self._load_agents(ctx.profile),
        )

        reporter = self.gateway.progress(
            target, f"Task {task.id}: {task.describe()}") if self.gateway else None
        on_progress = reporter.update if reporter else None

        result = await engine.run(task, spec, on_progress=on_progress)

        # Deliver, persist session, extract memory.
        summary = self._summarize(result)
        if reporter:
            await reporter.finish(summary)
        elif target is not None:
            await self.gateway.send(target, summary)

        if result.session_id:
            try:
                store.save_session(task.id, result.session_id,
                                   project=(target.key() if target else str(ctx.profile.workspace)),
                                   summary=(result.final_text or "")[:200],
                                   profile=task.profile)
            except Exception:
                self.log.debug("save_session failed", exc_info=True)

        if not result.is_error:
            try:
                async def llm(system: str, prompt: str) -> str:
                    return await engine.quick(system, prompt)
                await extract_and_store(store, task.profile, task, result, llm=llm)
            except Exception:
                self.log.debug("memory extraction failed", exc_info=True)

        return result

    def _load_agents(self, profile: Profile) -> Optional[dict]:
        """Optional subagent personas defined per profile in agents.json."""
        import json
        from claude_agent_sdk import AgentDefinition
        path = profile.root / "agents.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            return {name: AgentDefinition(**spec) for name, spec in raw.items()}
        except Exception:
            self.log.warning("invalid agents.json; ignoring")
            return None

    @staticmethod
    def _summarize(result: RunResult) -> str:
        if result.is_error:
            return f"Task failed: {result.error or 'unknown error'}"
        text = (result.final_text or "(no output)").strip()
        return f"{text}\n\n[done: ${result.cost_usd:.4f}, {result.num_turns} turns]"

    async def _deliver_result(self, task: Task, result: RunResult, msg: str) -> None:
        if self.gateway and task.channel_target is not None:
            await self.gateway.send(task.channel_target, msg)

    # ----- control plane ---------------------------------------------------

    async def handle_message(self, msg: InboundMessage) -> None:
        target = msg.target()
        profile_name = self._conv_profile.get(target.key(), self.active_profile)
        text = msg.text.strip()

        if text.startswith("/"):
            await self._handle_command(text[1:], target, profile_name, msg)
            return

        if not self.enqueue:
            await self.gateway.send(target, "Agent is not accepting tasks right now.")
            return
        task = Task(prompt=text, source=TaskSource.CHANNEL, channel_target=target,
                    profile=profile_name, sender_id=msg.sender_id)
        await self.enqueue(task)
        await self.gateway.send(target, f"Queued: {task.describe()}")

    async def _handle_command(self, body: str, target: ChannelTarget,
                              profile_name: str, msg: InboundMessage) -> None:
        parts = body.split()
        cmd = parts[0].lower() if parts else "help"
        args = parts[1:]
        ctx = self.context(profile_name)
        send = lambda t: self.gateway.send(target, t)

        if cmd == "help":
            await send(CHEAT_SHEET)
        elif cmd == "mode":
            if args:
                mode = ApprovalMode.coerce(args[0], None)
                if mode is None:
                    await send("usage: /mode [bypass|auto|ask]")
                else:
                    ctx.router.set_mode(mode)
                    await send(f"approval mode set to {mode.value}")
            else:
                await send(f"approval mode: {ctx.router.state.approval_mode.value}")
        elif cmd == "status":
            await send(self._status_text(ctx))
        elif cmd in ("pause", "resume"):
            ctx.router.state.paused = (cmd == "pause")
            ctx.router.state.save()
            await send(f"goal loop {'paused' if cmd == 'pause' else 'resumed'}")
        elif cmd == "cmd":
            await self._cmd_dispatch(args, target, profile_name, ctx)
        elif cmd == "mcp":
            servers = ctx.mcp.list()
            await send("MCP servers: " + (", ".join(servers) if servers else "none"))
        elif cmd == "skills":
            sk = ctx.skills.list()
            await send("Skills: " + (", ".join(s["name"] for s in sk) if sk else "none"))
        elif cmd == "profile":
            await self._profile_dispatch(args, target, send)
        else:
            await send(f"unknown command /{cmd}\n\n{CHEAT_SHEET}")

    async def _cmd_dispatch(self, args, target, profile_name, ctx) -> None:
        send = lambda t: self.gateway.send(target, t)
        if not args or args[0] == "list":
            cmds = ctx.commands.list()
            await send("Commands: " + (", ".join(c.name for c in cmds) if cmds else "none"))
            return
        name = args[0]
        cmd_args = {}
        bare = []
        for tok in args[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                cmd_args[k] = v
            else:
                bare.append(tok)
        if bare:
            cmd_args.setdefault("_", " ".join(bare))
        if not self.enqueue:
            await send("Agent is not accepting tasks right now.")
            return
        task = Task(command_ref=name, command_args=cmd_args, source=TaskSource.CHANNEL,
                    channel_target=target, profile=profile_name)
        await self.enqueue(task)
        await send(f"Queued command: {name}")

    async def _profile_dispatch(self, args, target, send) -> None:
        if not args or args[0] == "list":
            await send("Profiles: " + ", ".join(self.pm.list()))
        elif args[0] == "switch" and len(args) > 1:
            name = args[1]
            try:
                self.pm.get(name)
                self._conv_profile[target.key()] = name
                await send(f"this conversation now targets profile '{name}'")
            except FileNotFoundError:
                await send(f"no such profile '{name}'")
        else:
            await send("usage: /profile [list|switch <name>]")

    def _status_text(self, ctx: ProfileContext) -> str:
        q = self.queue.snapshot() if self.queue else {}
        cost = ctx.engine.cost.summary()
        return (f"profile: {ctx.profile.name}\n"
                f"mode: {ctx.router.state.approval_mode.value}"
                f"{' (paused)' if ctx.router.state.paused else ''}\n"
                f"queue: {len(q.get('running', []))} running, "
                f"{len(q.get('pending', []))} pending\n"
                f"cost today: ${cost['today']:.4f} (daily cap ${self.config.budgets.daily_usd})")
