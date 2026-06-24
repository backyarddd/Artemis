# Deployment

Artemis runs as an always-on gateway: `python -m artemis gateway run`. The
supervisor starts the channel gateway, task queue worker, cron scheduler,
optional webhook server, optional OpenAI endpoint, and the autonomous goal loop,
and shuts them all down cleanly on SIGTERM (stop accepting, drain the running
task, flush state, exit).

`artemis gateway install` detects the OS and installs the matching service.
`artemis gateway status|stop|restart|uninstall` work the same on every platform.

## macOS (launchd)

```bash
artemis gateway install            # writes ~/Library/LaunchAgents/com.artemis.agent.plist and loads it
artemis gateway status
artemis gateway restart
artemis gateway uninstall
```

The LaunchAgent uses `RunAtLoad` + `KeepAlive` (restart on crash, survive
sleep/wake) and writes logs under `$ARTEMIS_HOME`.

## Linux (systemd user unit)

```bash
artemis gateway install            # writes ~/.config/systemd/user/artemis.service and enables it
loginctl enable-linger "$USER"           # so it runs without an active login session
artemis gateway status
```

The unit uses `Restart=on-failure`.

## Docker

```bash
artemis gateway install --method docker   # generates Dockerfile, docker-compose.yml, .env.example
cp .env.example .env                            # fill in secrets
docker compose up -d --build
```

The image is `python:3.11-slim` plus Node 20 (the SDK bundles a node-based
`claude` CLI), with the project synced via uv. `ARTEMIS_HOME` is `/data`
(a mounted volume). Provide auth and channel secrets via `.env`
(`ANTHROPIC_API_KEY`, `ARTEMIS_TELEGRAM_TOKEN`, `ARTEMIS_DISCORD_TOKEN`,
`ARTEMIS_WEBHOOK_SECRET`, `ARTEMIS_OPENAI_API_KEY`).

To expose the OpenAI endpoint from the container, set
`openai_server.host: 0.0.0.0` in config and uncomment the `ports:` mapping in
`docker-compose.yml`.

In a container without your macOS keychain, supply `ANTHROPIC_API_KEY` (the
subscription bridge is macOS/Linux-file based and will not be available).

## Non-interactive provisioning

For headless servers and Docker, drive the whole wizard from an answers file or
environment variables:

```bash
artemis setup -n --answers answers.yaml
```

Secrets in the answers file are optional; they can come from environment
variables instead. See [`answers.example.yaml`](../answers.example.yaml).

## Verifying a deployment

```bash
artemis doctor      # isolation proof + auth + budgets
artemis gateway status
```
