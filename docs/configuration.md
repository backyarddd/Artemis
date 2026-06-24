# Configuration

All Artemis state lives under `$ARTEMIS_HOME` (default `~/.artemis`; the Docker
image uses `/data`). Global settings are in `config.yaml`; secrets never go in
that file.

## config.yaml

```yaml
default_profile: default          # active profile when ARTEMIS_PROFILE is unset
default_approval_mode: auto        # bypass | auto | ask
default_model: claude-sonnet-4-6   # model used for tasks unless overridden

# Per channel, the sender ids permitted to command the agent. CLI is always
# trusted. An unconfigured or empty allowlist denies that channel.
channel_allowlists:
  telegram: ["123456789"]
  discord: ["987654321"]

# Per channel, where proactive output (cron, goal loop) is delivered.
channel_defaults:
  telegram: "123456789"

budgets:
  per_task_usd: 1.0                # cap per task
  daily_usd: 20.0                  # cap per day; tasks are skipped past this

approval:
  timeout_seconds: 300             # how long a gated call waits for a decision
  default_action: deny             # action on timeout: deny | allow

webhook:
  enabled: false
  host: 127.0.0.1
  port: 8787

goal_loop:
  enabled: false
  interval_minutes: 30
  reflect_every: 5                 # every N cycles, run a reflection task

openai_server:
  enabled: false
  host: 127.0.0.1
  port: 8799
  require_auth: true               # require a Bearer key matching the stored secret

log_level: INFO
```

Edit it by hand, or use the CLI (`artemis mode`, `artemis profile switch`,
`artemis setup` to reconfigure interactively).

## Secrets

Secrets resolve from environment variables first, then the OS keyring. They are
never written to `config.yaml`.

Environment variable forms (checked in order):

```
ARTEMIS_<PROFILE>_<KEY>     e.g. ARTEMIS_DEFAULT_TELEGRAM_TOKEN
ARTEMIS_<KEY>               e.g. ARTEMIS_TELEGRAM_TOKEN
```

Keyring entries use service `artemis`, username `<profile>:<key>`.

Known keys:

| Key | Used for |
|-----|----------|
| `anthropic_api_key` | Use an API key instead of the subscription bridge |
| `telegram_token` | Telegram bot token |
| `discord_token` | Discord bot token |
| `webhook_secret` | HMAC secret for inbound webhooks |
| `openai_api_key` | Bearer key for the OpenAI-compatible endpoint |

The setup wizard and the relevant sub-wizards (`artemis channel add`,
`artemis serve --rotate-key`) store these for you.

## Profiles

Each profile is a fully isolated persona. `ARTEMIS_PROFILE` overrides the active
profile for a process. Create and switch with:

```bash
artemis profile create work --persona "Work Assistant"
artemis profile list
artemis profile switch work
```

A profile tree (`$ARTEMIS_HOME/profiles/<name>/`):

```
claude-home/   CLAUDE_CONFIG_DIR: isolated settings, skills, sessions, creds
workspace/     cwd; CLAUDE.md + .claude/skills/
mcp.json       Artemis-only MCP servers (strict-loaded)
settings.json  Artemis-only settings
soul.md        persona, appended to the system prompt
goals.md       standing goals for the autonomous loop
commands/      command templates (+ pending/ for agent-authored)
crons.json     scheduled jobs
state.json     approval mode + persistent allowlist + paused flag
artemis.db     memory + sessions (SQLite + FTS5)
logs/          artemis.log, audit.jsonl, cost.json
```

## Per-task and per-command overrides

- A command's frontmatter may set `approval_mode` and `allowed_tools` (a scoping
  restriction applied via the SDK `tools` field).
- A cron job or webhook payload may set its own `approval_mode` and target
  channel.
- The model can be overridden per task; otherwise `default_model` applies.
