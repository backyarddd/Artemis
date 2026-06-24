# Artemis Agent

A host-agnostic, always-on autonomous agent harness. Artemis uses the
**Claude Agent SDK** as its reasoning core, runs with a fully isolated Claude
config so it never touches the operator's personal Claude Code setup, is
controlled through a pluggable multi-channel gateway with live progress and
approval buttons, and is configured end to end through a guided setup wizard.

## What it does

- **Isolated brain.** Each profile gets its own `CLAUDE_CONFIG_DIR`, workspace,
  MCP set, skills, commands, and session history. The operator's `~/.claude`
  is never read or written. `artemis doctor` proves this on every run.
- **Reuses your subscription.** By default Artemis bridges your existing
  `claude login` session into the isolated profile. `ANTHROPIC_API_KEY`
  overrides it.
- **Multi-channel gateway.** One process fans out to many channels behind a
  single `ChannelAdapter`. v1 ships CLI, Telegram, and Discord. Adding a
  channel is a drop-in adapter; core never references a specific channel.
- **Three approval modes.** `bypass` (auto-approve), `auto` (gate risky, allow
  safe), `ask` (gate everything). Switchable live from any channel with
  `/mode`. Risky actions prompt with inline buttons (Approve / Deny / Always).
- **Catastrophe guards.** Mode-independent `PreToolUse` hooks that deny even
  under bypass: no writes/reads to the operator's `~/.claude`, `~/.claude.json`,
  or `~/.hermes`; no writes outside the profile tree; no `rm -rf` escapes,
  `dd`, fork bombs, or writes to service units / shell rc files.
- **Four triggers.** Cron schedule, on-demand channel command, inbound HMAC
  webhook, and an autonomous goal loop, all feeding one task queue.
- **Extensible.** Per-profile Commands (markdown templates), Skills, and MCP
  servers, all manageable from any channel and by the agent itself. The agent
  can author a new command that lands pending one-tap approval.
- **Memory.** SQLite + FTS5 recall injected into the system prompt, with
  post-task durable-fact extraction and per-conversation session resume.
- **OpenAI-compatible endpoint.** An optional HTTP server exposes
  `/v1/chat/completions` and `/v1/models`, so any program that speaks the OpenAI
  API can use your Claude subscription as a drop-in backend (streaming included).
- **Deploys anywhere.** First-class installers for launchd (macOS), systemd
  (Linux user unit), and Docker. Clean SIGTERM drain everywhere.

## Quick start

Requires Python 3.11+ and either a `claude login` session or an
`ANTHROPIC_API_KEY`.

```bash
uv sync
uv run artemis setup            # guided wizard (re-runnable)
uv run artemis doctor           # health + isolation proof
uv run artemis run "summarize today's git activity in this workspace"
uv run artemis daemon run -f    # run the daemon in the foreground
uv run artemis daemon install   # install + start the OS service
```

Headless / Docker provisioning (no prompts):

```bash
uv run artemis setup -n --answers answers.yaml
```

See `answers.example.yaml` for the schema (profile, auth, approval mode,
channels, MCP, goals, crons, budgets, service).

## Commands

CLI: `setup`, `run`, `serve`, `doctor`, `mode`, `daemon`, `profile`, `channel`,
`mcp`, `skill`, `cmd`, `cron`, `memory`. Run `uv run artemis --help` or
`uv run artemis <group> --help`.

From any channel: `/help` `/mode [bypass|auto|ask]` `/status` `/pause`
`/resume` `/cmd <name> k=v` `/cmd list` `/mcp` `/skills` `/profile`.

## OpenAI-compatible endpoint

Use Artemis as a drop-in OpenAI backend for any program. Enable it in `artemis
setup` (or run it standalone):

```bash
uv run artemis serve                 # foreground; prints base_url + API key
uv run artemis serve --no-auth       # localhost, no key
uv run artemis serve --port 8799 --rotate-key
```

It also auto-starts with the daemon when `openai_server.enabled` is set. Then
point any OpenAI client at it:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8799/v1", api_key="sk-artemis-...")
client.chat.completions.create(
    model="claude-sonnet-4-6",        # or any name; non-Claude names map to a default
    messages=[{"role": "user", "content": "Hello"}],
)
```

Endpoints: `POST /v1/chat/completions` (streaming and non-streaming) and
`GET /v1/models`. Bind host/port and auth are configurable; default is
`127.0.0.1` with a required Bearer key generated at setup. Requests run fully
inside the isolated profile with no tools (pure text generation).

It is built on the Claude Agent SDK, so it is a faithful **text** completion
proxy but not a full parameter passthrough: `temperature`, `top_p`,
`max_tokens`, `stop`, `n`, `seed`, penalties, `response_format`, and client-side
`tools`/function-calling are accepted (never rejected) but not honored. Unknown
fields and headers are ignored, per OpenAI client expectations.

## Architecture

```
trigger (cron | channel | webhook | goal loop)
        -> Task -> TaskQueue -> Orchestrator.run_task
                                   |-- resolve saved command template
                                   |-- recall memory (FTS5) into system prompt
                                   |-- Engine: ClaudeAgentOptions + ClaudeSDKClient
                                   |     |-- can_use_tool  -> ApprovalRouter (3 modes)
                                   |     |-- hooks         -> catastrophe guards
                                   |     '-- stream        -> Gateway progress (edit-in-place)
                                   |-- persist session for resume
                                   '-- extract durable facts
```

Modules: `engine` (SDK wrapper + stream router), `approval` (three-mode
can_use_tool), `hooks` (catastrophe guards), `orchestrator` (runner + control
plane), `queue`, `channels/` (gateway + adapters), `triggers/`, `memory/`,
`registry/`, `profiles`, `service/`, `daemon`, `setup`, `doctor`.

Profile tree on disk (`$ARTEMIS_HOME`, default `~/.artemis`):

```
~/.artemis/
  config.yaml
  profiles/default/
    claude-home/      CLAUDE_CONFIG_DIR (isolated settings, skills, sessions, creds)
    workspace/        cwd; CLAUDE.md + .claude/skills/
    mcp.json          Artemis-only MCP servers (strict-loaded)
    settings.json     Artemis-only settings
    soul.md           persona, appended to the system prompt
    goals.md          standing goals for the autonomous loop
    commands/         command templates (+ pending/ for agent-authored)
    crons.json        scheduled jobs
    state.json        approval mode + persistent allowlist + paused flag
    artemis.db        memory + sessions
    logs/             artemis.log, audit.jsonl, cost.json
```

## Design decisions and assumptions

These were decided during the build (the brief instructed: decide and note
assumptions here).

- **Auth bridge.** On macOS the `claude login` token lives in the login
  keychain (`Claude Code-credentials`), not a file, and relocating
  `CLAUDE_CONFIG_DIR` cuts it off. Artemis copies **only** the `claudeAiOauth`
  block (never the operator's `mcpOAuth` tokens) into
  `claude-home/.credentials.json` (mode 0600), plus the account identity into
  `claude-home/.claude.json`. On Linux it reads the operator's file-based
  `~/.claude/.credentials.json`. The token self-refreshes inside the profile
  thereafter. `ANTHROPIC_API_KEY` (env, or a keyring secret saved during setup)
  bypasses bridging entirely.
- **Isolation.** `CLAUDE_CONFIG_DIR` relocation + `strict_mcp_config=True` +
  `setting_sources=["user","project"]`. Because the config dir is relocated,
  the "user" layer resolves to the profile's own claude-home, so Artemis's own
  skills load while the operator's do not. `doctor` asserts empirically that
  none of the operator's custom skills/agents/MCP appear in `system/init`.
- **Streaming.** `can_use_tool` requires streaming mode, so the engine drives
  `ClaudeSDKClient`, not one-shot `query()`. The system prompt extends the
  `claude_code` preset (append) rather than replacing it.
- **Approval policy.** `ask` gates every call; the persistent allowlist
  short-circuits only in `auto`. In `auto`, Bash is allowed unless its command
  matches the risky set (git mutations, rm/mv/dd, installs, pipe-to-shell,
  network, sudo, credential paths). Approval timeouts default to deny.
- **Cron storage.** Jobs persist in `crons.json` (decoupled from the memory
  db). CLI changes to cron/MCP/commands take effect on the next daemon start;
  there is no live control socket in v1.
- **Memory extraction.** Each successful task triggers one cheap haiku one-shot
  to distill durable facts; this is bounded by the daily budget cap.
- **Default model** is `claude-sonnet-4-6`; doctor and auxiliary calls use
  haiku. Override per task or via `config.yaml`.
- **Secrets** resolve from env (`ARTEMIS_<KEY>` or `ARTEMIS_<PROFILE>_<KEY>`)
  first, then the OS keyring. Tokens never land in `config.yaml`.

## Known limitations

- The bash catastrophe guard is defense-in-depth, not a sandbox. It expands
  `~`/env vars, tracks `cd`, denies sacred-path references and unanalyzable
  destructive commands, but path-based shell analysis is inherently
  incomplete. For untrusted workloads, run the daemon in the Docker image or
  an OS sandbox. Risky bash is also gated by the approval router in auto/ask.
- CLI approvals use a blocking stdin read, intended for foreground use. In a
  headless daemon an approval routed to the CLI channel fails safe (times out
  to deny); use Telegram/Discord for interactive approvals on a server.

## Deferred to a later phase

Self-improving skills/commands (reflection proposing `skills/auto/*` pending
approval) and a BajaClaw/Hermes config importer are out of v1 scope.

## Development

```bash
uv run pytest            # unit + integration suite (no network)
uv run artemis doctor    # live isolation proof (uses your session)
```

House style: no em dashes, conventional commits, no AI attribution in git.
