# Artemis Agent

A host-agnostic, always-on autonomous agent harness. Artemis uses the
**Claude Agent SDK** as its reasoning core, runs with a fully isolated Claude
config so it never touches your personal Claude Code setup, is controlled
through a pluggable multi-channel gateway with live progress and approval
buttons, and is configured end to end through a guided setup wizard. It can also
expose your Claude subscription as a drop-in **OpenAI-compatible HTTP endpoint**
for any other program.

```
trigger (cron | channel | webhook | goal loop)
        -> Task -> queue -> orchestrator
                              |- resolve a saved command template
                              |- recall memory (SQLite FTS5) into the system prompt
                              |- engine: ClaudeAgentOptions + ClaudeSDKClient (isolated)
                              |     |- can_use_tool  -> 3-mode approval router
                              |     |- hooks         -> catastrophe guards
                              |     '- stream        -> edit-in-place channel progress
                              |- persist the session for resume
                              '- extract durable facts
```

## Contents

- [Why](#why)
- [Features](#features)
- [Install](#install)
- [Quick start](#quick-start)
- [The OpenAI-compatible endpoint](#the-openai-compatible-endpoint)
- [Approval model](#approval-model)
- [Triggers](#triggers)
- [Extensibility: commands, skills, MCP](#extensibility-commands-skills-mcp)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Security model](#security-model)
- [Command reference](#command-reference)
- [Project layout](#project-layout)
- [Development](#development)
- [Limitations](#limitations)
- [License](#license)

## Why

You already pay for a Claude subscription via `claude login`. Artemis turns that
into a always-on autonomous agent you can reach from chat, schedule, trigger
from webhooks, or call as an OpenAI API, without ever exposing or mutating your
personal Claude Code environment. One install can host several isolated agent
personas ("profiles"), each with its own workspace, tools, memory, and channels.

## Features

- **Isolated brain.** Each profile gets its own `CLAUDE_CONFIG_DIR`, workspace,
  MCP set, skills, commands, and session history. Your real `~/.claude` is never
  read or written. `artemis doctor` proves this on every run.
- **Reuses your subscription.** Artemis bridges your existing `claude login`
  session into the isolated profile. `ANTHROPIC_API_KEY` overrides it.
- **Multi-channel gateway.** One process fans out to many channels behind a
  single `ChannelAdapter`. Ships CLI, Telegram, and Discord. Adding a channel is
  a drop-in adapter; core never references a specific channel.
- **Three approval modes.** `bypass` (auto-approve), `auto` (gate risky, allow
  safe), `ask` (gate everything). Switchable live from any channel with `/mode`.
  Risky actions prompt with inline buttons (Approve / Deny / Always allow).
- **Catastrophe guards.** Mode-independent `PreToolUse` hooks that deny even
  under bypass: no reads or writes to your `~/.claude`, `~/.claude.json`, or
  `~/.hermes`; no writes outside the profile tree; no `rm -rf` escapes, `dd`,
  fork bombs, or writes to service units / shell rc files.
- **Four triggers.** Cron schedule, on-demand channel command, inbound HMAC
  webhook, and an autonomous goal loop, all feeding one task queue.
- **Extensible.** Per-profile Commands (markdown templates), Skills, and MCP
  servers, all manageable from any channel and by the agent itself. The agent
  can author a new command that lands pending one-tap approval.
- **Memory.** SQLite + FTS5 recall injected into the system prompt, with
  post-task durable-fact extraction and per-conversation session resume.
- **OpenAI-compatible endpoint.** Optional HTTP server exposing
  `/v1/chat/completions` and `/v1/models`, so any OpenAI client uses your Claude
  subscription as a drop-in backend (streaming included).
- **Deploys anywhere.** First-class installers for launchd (macOS), systemd
  (Linux user unit), and Docker. Clean SIGTERM drain everywhere.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and either a
`claude login` subscription session or an `ANTHROPIC_API_KEY`. The `claude` CLI
ships bundled with the SDK; no separate install is needed.

```bash
git clone https://github.com/backyarddd/artemis.git
cd artemis
uv sync
```

## Quick start

```bash
uv run artemis setup            # guided wizard (re-runnable any time)
uv run artemis doctor           # health + isolation proof
uv run artemis run "summarize today's git activity in this workspace"
uv run artemis daemon run -f    # run the daemon in the foreground
uv run artemis daemon install   # install + start the OS service
```

Headless / Docker provisioning with no prompts:

```bash
uv run artemis setup -n --answers answers.yaml
```

See [`answers.example.yaml`](answers.example.yaml) for the full schema.

## The OpenAI-compatible endpoint

Use Artemis as a drop-in OpenAI backend for any program. Enable it during
`artemis setup`, or run it standalone:

```bash
uv run artemis serve                 # foreground; prints base_url + API key
uv run artemis serve --no-auth       # localhost, no key
uv run artemis serve --port 8799 --rotate-key
uv run artemis serve --print-key     # print the current API key and exit
```

It also auto-starts with the daemon when `openai_server.enabled` is set. Point
any OpenAI client at it:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8799/v1", api_key="sk-artemis-...")
resp = client.chat.completions.create(
    model="claude-sonnet-4-6",        # any name works; non-Claude names map to a default
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)

# streaming
for chunk in client.chat.completions.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}], stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

It works unmodified with the `openai` SDK, LangChain's `ChatOpenAI`
(`base_url=".../v1"`), and `curl`. Endpoints: `POST /v1/chat/completions`
(streaming and non-streaming) and `GET /v1/models`. See
[docs/openai-endpoint.md](docs/openai-endpoint.md) for the full contract and the
parameter-support caveats.

## Approval model

Every tool call the agent wants to make passes through one `can_use_tool`
callback that implements three runtime-switchable modes:

| Mode | Behavior |
|------|----------|
| `bypass` | Auto-approve everything. Catastrophe guards still hard-deny. |
| `auto` (default) | Auto-allow read-only tools and safe Bash; gate writes, deletes, network, installs, git mutations, credential access, and anything unrecognized. |
| `ask` | Gate every tool call. |

Gated calls push an inline-keyboard prompt (Approve / Deny / Always allow) to
the active channel and wait. "Always allow" persists the exact tool or command
to a per-profile allowlist. On channels without buttons (CLI) the same code path
falls back to a typed `y/n/a` prompt. Timeouts default to deny. Every decision,
automatic or manual, is written to the audit log. Switch live from any channel
with `/mode bypass|auto|ask`.

## Triggers

All four normalize to a `Task` and enqueue onto one bounded-concurrency queue:

- **Cron** (`triggers/scheduler.py`): APScheduler jobs from `crons.json`, each
  with a prompt or command, schedule, target channel, and approval mode.
- **On-demand**: any allowlisted message or `/cmd` from a channel.
- **Webhook** (`triggers/webhooks.py`): FastAPI + HMAC-verified endpoints; a
  generic `POST /webhook/{name}` plus a GitHub-style example.
- **Goal loop** (`triggers/goal_loop.py`): a heartbeat that advances the next
  standing goal from `goals.md`, with a periodic reflection cycle. Pausable with
  `/pause`.

## Extensibility: commands, skills, MCP

All three are per profile, manageable from any channel and the CLI, and usable
by the agent itself.

- **Commands** are markdown templates with YAML frontmatter (`name`,
  `description`, `args`, optional `allowed_tools`, `approval_mode`,
  `default_channel`) and a body with `{arg}` placeholders. Invoke as
  `/cmd <name> k=v` from a channel or `artemis cmd run <name>`. The agent can
  author new commands; they land pending and are activated with
  `/cmd approve <name>`. A starter pack (`research`, `ship`, `standup`, `track`,
  `digest`) is seeded at setup.
- **Skills** live under the profile and are managed with `artemis skill
  add|list|remove`. User-authored skills are never overwritten.
- **MCP servers** are stored per profile in `mcp.json`, strict-loaded so nothing
  global leaks. Manage with `artemis mcp add|list|remove|test`.

## Configuration

Global config lives at `$ARTEMIS_HOME/config.yaml` (default `~/.artemis`).
Secrets (channel tokens, webhook secret, endpoint key) never land in the YAML:
they resolve from environment variables first, then the OS keyring. See
[docs/configuration.md](docs/configuration.md) for every field.

## Deployment

`artemis daemon install` detects the OS and installs the matching service:
launchd (macOS), systemd user unit (Linux), or Docker. All survive restarts and
shut down cleanly on SIGTERM. See [docs/deployment.md](docs/deployment.md).

## Security model

Isolation, catastrophe guards, the approval pipeline, the auth bridge, and the
endpoint auth posture are described in [docs/security.md](docs/security.md).
The short version: Artemis runs in its own relocated Claude config home, the
agent is hard-blocked from touching your real config, and risky actions are
gated unless you switch to bypass.

## Command reference

`setup`, `run`, `serve`, `doctor`, `mode`, `daemon`, `profile`, `channel`,
`mcp`, `skill`, `cmd`, `cron`, `memory`. Run `uv run artemis --help` or
`uv run artemis <group> --help`. Full reference in
[docs/commands.md](docs/commands.md).

From any channel: `/help` `/mode [bypass|auto|ask]` `/status` `/pause`
`/resume` `/cmd <name> k=v` `/cmd list` `/cmd pending` `/cmd approve <name>`
`/mcp` `/skills` `/profile [list|switch <name>]`.

## Project layout

```
artemis/
  __main__.py        CLI entrypoint (typer)
  setup.py           guided wizard + non-interactive mode
  config.py          global config + paths + secrets
  profiles.py        isolated profile tree + subscription auth bridge
  engine.py          ClaudeAgentOptions builder + streamed run + stream router
  approval.py        3-mode approval router + can_use_tool
  hooks.py           catastrophe guards (PreToolUse)
  orchestrator.py    task runner + channel control plane
  queue.py           async task queue with persistence
  observability.py   logging, audit trail, cost tracking
  doctor.py          health + isolation proof
  daemon.py          supervisor (gateway, queue, triggers, endpoint)
  channels/          base, gateway, cli, telegram, discord
  triggers/          scheduler, webhooks, goal_loop
  memory/            store (SQLite+FTS5), recall, extract
  registry/          commands, skills, mcp
  service/           install, launchd, systemd, docker
  server/            openai_api (OpenAI-compatible endpoint)
```

Each profile on disk (`$ARTEMIS_HOME/profiles/<name>/`) holds its own
`claude-home/`, `workspace/`, `mcp.json`, `settings.json`, `soul.md`,
`goals.md`, `commands/`, `crons.json`, `state.json`, `artemis.db`, and `logs/`.

## Development

```bash
uv run pytest            # full unit + integration suite (no network)
uv run artemis doctor    # live isolation proof (uses your session)
```

House style: no em dashes, conventional commits, no AI attribution in git.

## Limitations

- The bash catastrophe guard is defense-in-depth, not a sandbox. For untrusted
  workloads, run the daemon in the Docker image or an OS sandbox.
- CLI approvals use a blocking stdin read, intended for foreground use. On a
  headless daemon an approval routed to the CLI channel fails safe (times out to
  deny); use Telegram/Discord for interactive approvals on a server.
- The OpenAI endpoint is built on the Agent SDK, so it is a faithful text
  completion proxy but not a full parameter passthrough: sampling params
  (`temperature`, `max_tokens`, `top_p`, `stop`, `seed`, penalties) and
  client-side tool/function calling are accepted but not honored.

Deferred to a later phase: self-improving skills/commands (reflection proposing
`skills/auto/*` pending approval) and a config importer.

## License

MIT. See [LICENSE](LICENSE).
