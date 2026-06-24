# Security model

Artemis is designed so an always-on autonomous agent can run on your machine
without ever touching or exposing your personal Claude Code environment, and so
that risky actions are gated by default.

## Config isolation

Every run is fully isolated from your real `~/.claude`:

- **`CLAUDE_CONFIG_DIR`** is relocated to the profile's `claude-home/`. This
  moves settings, `skills/`, agents, commands, and session history. Your
  personal skills and agents do not load.
- **`strict_mcp_config=True`** skips the global `~/.claude.json` MCP merge; only
  the profile's `mcp.json` servers load.
- **`setting_sources`** is scoped so your user-level allow/deny rules do not
  leak in.
- **`cwd`** is the profile workspace; **`settings`** points at the profile's own
  settings file.

`artemis doctor` proves this empirically: it runs a real task and asserts from
the `system/init` message that none of your custom skills, agents, or MCP
servers appear, and that the loaded set is only built-ins plus the profile's
own. It fails loudly on any contamination.

## Catastrophe guards

Mode-independent `PreToolUse` hooks (`hooks.py`) deny dangerous calls even under
bypass. They are the hard floor beneath the approval router:

- No reads or writes to `~/.claude`, `~/.claude.json`, or `~/.hermes` (case- and
  unicode-normalized; `$HOME`/`~` expanded so they cannot be smuggled in).
- No writes outside the profile tree (configurable allowlist for extra dirs).
- No `rm -rf` / `dd` / fork bombs that escape the workspace; `cd` is tracked
  across compound commands so `cd ~ && rm -rf .claude` is caught.
- No writes to service unit files or shell rc files (except during setup).
- Bash that still contains unresolved shell expansion in a destructive command
  is denied rather than guessed at.

This is defense-in-depth, not a kernel sandbox. For untrusted workloads, run in
the Docker image or an OS sandbox.

## Approval pipeline

The SDK resolves every tool call in a fixed order: PreToolUse hooks, deny rules,
permission mode, allow rules, then the `can_use_tool` callback. Artemis keeps
`permission_mode="default"` and makes `can_use_tool` authoritative: the SDK
`allowed_tools` list is left empty so no tool can skip gating (an allow rule
would bypass the callback even in `ask` mode). The three modes are implemented
inside that one callback. "Always allow" persists the **exact** normalized
command (for Bash) or the tool name to a per-profile allowlist.

## Subscription auth bridge

On macOS your `claude login` token lives in the login keychain (service
`Claude Code-credentials`), not a file, so relocating `CLAUDE_CONFIG_DIR` cuts
it off. Artemis copies **only** the `claudeAiOauth` block (never your `mcpOAuth`
tokens) into the profile's `claude-home/.credentials.json` (mode `0600`), plus
the account identity into `claude-home/.claude.json`. On Linux it reads the
file-based `~/.claude/.credentials.json`. The token self-refreshes inside the
profile thereafter. Setting `ANTHROPIC_API_KEY` (env, or a keyring secret)
bypasses bridging entirely.

## Channel allowlists

Inbound channel messages are dropped unless the sender id is on the per-channel
allowlist (CLI is always trusted as local). An unconfigured or empty allowlist
denies the channel.

## OpenAI endpoint auth

The endpoint defaults to binding `127.0.0.1` with `require_auth: true` and a
Bearer key. The auth check **fails closed**: if auth is required but no key is
configured, requests are rejected with `503` rather than served open. The gateway
provisions and persists a key on start when one is missing (retrieve it with
`artemis serve --print-key`). Exposing the endpoint beyond localhost requires
setting a non-loopback `host` deliberately; keep `require_auth` on if you do.

## Audit and cost

Every tool decision (manual or automatic), hook block, and task lifecycle event
is written to `logs/audit.jsonl`. Per-task and daily cost is tracked from each
run's result and enforced against the configured budgets. `/status` and
`artemis doctor` surface this.

## Reporting

This is a personal tool. If you find a security issue, open an issue or fix it
and send a PR.
