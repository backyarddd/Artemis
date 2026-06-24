# OpenAI-compatible endpoint

Artemis can expose your Claude subscription as a drop-in OpenAI API. Any program
that speaks the OpenAI Chat Completions protocol can point at it and get Claude
responses, with streaming, fully inside the isolated profile and with no tools
(pure text generation).

## Running it

```bash
artemis serve                    # foreground; prints base_url + API key + a curl example
artemis serve --no-auth          # localhost, no key (convenient for local dev)
artemis serve --port 8799        # choose the port
artemis serve --rotate-key       # generate a fresh API key
artemis serve --print-key        # print the current key and exit
```

It also auto-starts with the daemon when `openai_server.enabled: true`. Configure
host/port/auth under `openai_server` in `config.yaml` (see
[configuration.md](configuration.md)). Default bind is `127.0.0.1` with a
required Bearer key.

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| POST | `/v1/chat/completions` | Streaming and non-streaming |
| GET  | `/v1/models` | Lists the served Claude models |
| GET  | `/health` | `{"ok": true}` |

Non-`/v1` variants (`/chat/completions`, `/models`) are also registered as a
fallback for clients whose base URL omits `/v1`.

## Clients

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8799/v1", api_key="sk-artemis-...")
client.chat.completions.create(model="claude-sonnet-4-6",
                               messages=[{"role": "user", "content": "Hello"}])
client.models.list()
```

```python
from langchain_openai import ChatOpenAI
llm = ChatOpenAI(model="claude-sonnet-4-6",
                 base_url="http://127.0.0.1:8799/v1", api_key="sk-artemis-...")
```

```bash
curl http://127.0.0.1:8799/v1/chat/completions \
  -H "Authorization: Bearer sk-artemis-..." -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"hi"}]}'
```

## Model names

You may pass any `model` string. Claude model ids pass through; common "small"
OpenAI names (e.g. `gpt-4o-mini`) map to a haiku tier; anything else falls back
to `default_model`. The response echoes the model name you requested.
`GET /v1/models` advertises the Claude models Artemis serves.

## Authentication

With `require_auth: true` (the default) the endpoint expects
`Authorization: Bearer <key>`. The key is generated at setup or via
`artemis serve --rotate-key`, stored in the keyring (or supplied via
`ARTEMIS_OPENAI_API_KEY`). Auth fails closed: if auth is required but no key is
configured, requests are rejected with `503`. Use `--no-auth` for trusted
localhost-only use.

## Wire format

Responses follow the OpenAI contract exactly: non-streaming returns a
`chat.completion` object with `choices[].message`, `finish_reason`, and a
`usage` object with real token counts; streaming returns
`text/event-stream` of `chat.completion.chunk` events (role-first chunk, content
deltas, a final `finish_reason` chunk, an optional `usage` chunk when
`stream_options.include_usage` is set) terminated by `data: [DONE]`. Errors use
the `{"error": {...}}` envelope with the correct HTTP status. Unknown request
fields and headers are ignored, never rejected.

## Parameter support (important)

The endpoint is built on the Claude Agent SDK, so it is a faithful **text**
completion proxy but **not** a full parameter passthrough. The following are
accepted (never rejected) but **not honored**:

- Sampling: `temperature`, `top_p`, `max_tokens` / `max_completion_tokens`,
  `stop`, `n` (always 1 choice), `seed`, `presence_penalty`,
  `frequency_penalty`, `logit_bias`, `logprobs`.
- `response_format` (JSON mode / schema) is not enforced.
- Client-side `tools` / `tool_choice` (function calling) are not executed; the
  agent runs with no tools, so it never returns `tool_calls`.

If you need full sampling control or function calling, use the Anthropic API
directly with an API key. The value of this endpoint is using your existing
subscription as a standard OpenAI backend for chat-style text generation.

## Concurrency

Each request runs a `claude` CLI subprocess inside the isolated profile.
Moderate concurrency is fine; very high request rates will spawn many
subprocesses.
