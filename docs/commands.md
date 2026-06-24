# Command reference

Run `artemis --help` or `artemis <group> --help` for live help.
All state-mutating commands operate on the active profile (set by
`ARTEMIS_PROFILE` or `default_profile`). Changes to cron, MCP, and commands take
effect on the next gateway start.

## Top-level

| Command | Description |
|---------|-------------|
| `artemis setup [-n] [--answers FILE]` | Guided first-run wizard; `-n` for headless/non-interactive |
| `artemis run "<prompt>" [--profile] [--mode]` | Run a single task in the foreground and print the result |
| `artemis serve [--host] [--port] [--no-auth] [--rotate-key] [--print-key]` | Run the OpenAI-compatible endpoint in the foreground |
| `artemis doctor [--profile] [--no-isolation]` | Health and isolation self-check |
| `artemis mode [bypass\|auto\|ask]` | Show or set the active profile's default approval mode |

## gateway

| Command | Description |
|---------|-------------|
| `artemis gateway run [--profile] [-f]` | Run the supervised gateway (used by service units) |
| `artemis gateway install [--method launchd\|systemd\|docker]` | Install + start the OS service |
| `artemis gateway status` | Service status |
| `artemis gateway stop` / `restart` / `uninstall` | Manage the service |

## profile

| Command | Description |
|---------|-------------|
| `artemis profile create <name> [--persona]` | Scaffold a new isolated profile |
| `artemis profile list` | List profiles (active marked) |
| `artemis profile switch <name>` | Set the default profile |

## channel

| Command | Description |
|---------|-------------|
| `artemis channel add <telegram\|discord>` | Sub-wizard to configure a channel |
| `artemis channel list` | Show configured channels |

## mcp

| Command | Description |
|---------|-------------|
| `artemis mcp list` | List per-profile MCP servers |
| `artemis mcp add <name> [--command] [--url] [--arg ...]` | Add a server |
| `artemis mcp remove <name>` | Remove a server |
| `artemis mcp test <name>` | Best-effort connectivity check |

## skill

| Command | Description |
|---------|-------------|
| `artemis skill list` | List per-profile skills |
| `artemis skill add <name> [--path DIR]` | Install a skill (never overwrites) |
| `artemis skill remove <name>` | Remove a skill |

## cmd

| Command | Description |
|---------|-------------|
| `artemis cmd list` | List saved commands |
| `artemis cmd new <name> [--desc] [--body]` | Scaffold a command template |
| `artemis cmd run <name> [k=v ...]` | Run a saved command |
| `artemis cmd approve <name>` | Approve an agent-authored (pending) command |
| `artemis cmd remove <name>` | Delete a command |

## cron

| Command | Description |
|---------|-------------|
| `artemis cron add "<5-field cron>" [--prompt] [--command]` | Add a scheduled job |
| `artemis cron list` | List jobs |
| `artemis cron remove <job_id>` | Remove a job |

## memory

| Command | Description |
|---------|-------------|
| `artemis memory search <query> [--limit]` | FTS5 search over stored facts |
| `artemis memory recent [--limit]` | Most recent facts |
| `artemis memory add <text>` | Store a fact manually |

## Channel commands

From any configured channel (sender must be allowlisted):

| Command | Description |
|---------|-------------|
| `/help` | Show the cheat sheet |
| `/mode [bypass\|auto\|ask]` | Show or set approval mode |
| `/status` | Queue, mode, and cost |
| `/pause` / `/resume` | Pause or resume the autonomous goal loop |
| `/cmd <name> k=v ...` | Run a saved command |
| `/cmd list` | List commands |
| `/cmd pending` | List agent-authored commands awaiting approval |
| `/cmd approve <name>` | Approve an agent-authored command |
| `/mcp` | List MCP servers |
| `/skills` | List skills |
| `/profile [list\|switch <name>]` | Manage the conversation's target profile |
| anything else | Run as a one-off task |
