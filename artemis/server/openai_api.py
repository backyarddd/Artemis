"""OpenAI-compatible HTTP endpoint: a drop-in LLM backend for other programs.

Point any OpenAI client at ``http://host:port/v1`` and it talks to Claude
through the operator's subscription, fully inside the isolated profile. Serves
the verified-minimal surface: POST /v1/chat/completions (streaming + not) and
GET /v1/models, plus non-/v1 fallbacks.

Built on the Claude Agent SDK, so sampling params (temperature, top_p,
max_tokens, stop, n, seed, penalties) and client-side tool/function calling are
accepted-but-not-honored: this is a text completion proxy. Unknown fields and
headers are ignored, never rejected, per OpenAI client expectations.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import GlobalConfig, get_secret, set_secret
from ..engine import Engine
from ..observability import get_logger
from ..profiles import Profile


def ensure_openai_key(profile_name: str, generate: bool = True) -> Optional[str]:
    """Return the endpoint API key, generating and persisting one if absent.

    Returns None only when no key exists and it could not be persisted (no
    keyring backend) - callers must then fail closed rather than serve open."""
    key = get_secret("openai_api_key", profile_name)
    if key or not generate:
        return key
    import secrets
    candidate = "sk-artemis-" + secrets.token_urlsafe(24)
    return candidate if set_secret("openai_api_key", candidate, profile_name) else None

# Claude models advertised by GET /v1/models (clients may pass any of these,
# or any custom string, as `model`).
SERVED_MODELS = [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]
_SMALL_HINTS = ("mini", "nano", "small", "3.5", "haiku", "fast", "lite")


def resolve_model(requested: Optional[str], default: str) -> str:
    """Map the client's model string to a Claude model id to actually run.

    Claude names pass through; common 'small' OpenAI names map to haiku; anything
    else falls back to the configured default. The response still echoes the
    requested string (drop-in behavior)."""
    if not requested:
        return default
    r = requested.lower()
    if "claude" in r or any(x in r for x in ("sonnet", "opus", "fable")):
        return requested
    if any(h in r for h in _SMALL_HINTS):
        return "claude-haiku-4-5-20251001"
    return default


def extract_text(content: Any) -> str:
    """Flatten OpenAI message content (string | parts array | null) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


_ROLE_LABELS = {"user": "User", "assistant": "Assistant", "tool": "Tool result",
                "function": "Tool result"}


def render_messages(messages: list[dict]) -> tuple[str, str]:
    """Split OpenAI messages into (system_prompt, user_prompt).

    System/developer messages become the system prompt. A single user turn maps
    straight to the prompt; multi-turn history is rendered as a labeled
    transcript so the model has full context."""
    system_parts: list[str] = []
    convo: list[tuple[str, str]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        text = extract_text(m.get("content"))
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
        else:
            convo.append((role, text))

    system = "\n\n".join(system_parts)
    if not convo:
        return system, "(no user message)"
    if len(convo) == 1:
        return system, convo[0][1]

    history = "\n".join(f"{_ROLE_LABELS.get(r, r.capitalize())}: {t}"
                        for r, t in convo[:-1])
    last_role, last_text = convo[-1]
    if last_role == "user":
        return system, (f"Conversation so far:\n{history}\n\n"
                        f"Respond to the latest message:\n{last_text}")
    # Trailing non-user turn: ask the model to continue.
    return system, (f"Conversation so far:\n{history}\n{_ROLE_LABELS.get(last_role, last_role)}: "
                    f"{last_text}\n\nContinue.")


# ---------------------------------------------------------------------------
# Response shapes (verified against the OpenAI API reference).
# ---------------------------------------------------------------------------

def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _now() -> int:
    return int(time.time())


def completion_response(cid: str, created: int, model: str, text: str,
                        finish_reason: str, usage: dict) -> dict:
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": finish_reason, "logprobs": None}],
        "usage": usage,
    }


def _chunk(cid: str, created: int, model: str, delta: dict,
           finish_reason: Optional[str] = None) -> dict:
    return {
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _usage_chunk(cid: str, created: int, model: str, usage: dict) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model, "choices": [], "usage": usage}


def _error(message: str, type_: str = "server_error", status: int = 500,
           param: Optional[str] = None, code: Optional[str] = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": type_, "param": param, "code": code}})


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj) + "\n\n"


class OpenAIServer:
    """Serves the OpenAI-compatible FastAPI app via uvicorn."""

    def __init__(self, profile: Profile, config: GlobalConfig):
        self.profile = profile
        self.config = config
        self.engine = Engine(profile, config)
        self.log = get_logger("artemis.openai", profile.logs_dir, config.log_level)
        self._api_key = get_secret("openai_api_key", profile.name)
        self.app = self._build_app()
        self._server: Optional[uvicorn.Server] = None
        self._serve_task = None
        self._started_at = _now()

    # ----- auth ------------------------------------------------------------

    def _check_auth(self, request: Request) -> Optional[JSONResponse]:
        if not self.config.openai_server.require_auth:
            return None
        if not self._api_key:
            # Fail closed: never serve open when auth was requested but no key
            # could be provisioned (e.g. no keyring backend).
            return _error("Endpoint requires auth but no API key is configured. "
                          "Run 'artemis serve --rotate-key' or set ARTEMIS_OPENAI_API_KEY.",
                          "authentication_error", 503)
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token != self._api_key:
            return _error("Invalid API key.", "authentication_error", 401)
        return None

    # ----- app -------------------------------------------------------------

    def _models_payload(self) -> dict:
        models = list(dict.fromkeys(SERVED_MODELS + [self.config.default_model]))
        return {"object": "list",
                "data": [{"id": m, "object": "model", "created": self._started_at,
                          "owned_by": "anthropic"} for m in models]}

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Artemis OpenAI-compatible API")
        server = self

        @app.get("/health")
        async def health() -> dict:
            return {"ok": True}

        async def models(_: Request) -> JSONResponse:
            return JSONResponse(server._models_payload())

        async def chat_completions(request: Request):
            auth_err = server._check_auth(request)
            if auth_err is not None:
                return auth_err
            try:
                body = await request.json()
            except Exception:
                return _error("Invalid JSON body.", "invalid_request_error", 400)
            if not isinstance(body, dict):
                return _error("Body must be a JSON object.", "invalid_request_error", 400)

            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                return _error("'messages' is required and must be a non-empty array.",
                              "invalid_request_error", 400, param="messages")

            requested_model = str(body.get("model") or server.config.default_model)
            run_model = resolve_model(requested_model, server.config.default_model)
            system, prompt = render_messages(messages)
            stream = bool(body.get("stream", False))
            so = body.get("stream_options")
            include_usage = bool(so.get("include_usage", False)) if isinstance(so, dict) else False

            if stream:
                return StreamingResponse(
                    server._stream(system, prompt, run_model, requested_model, include_usage),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                             "X-Accel-Buffering": "no"})
            return await server._complete(system, prompt, run_model, requested_model)

        for path in ("/v1/models", "/models"):
            app.add_api_route(path, models, methods=["GET"])
        for path in ("/v1/chat/completions", "/chat/completions"):
            app.add_api_route(path, chat_completions, methods=["POST"])
        return app

    # ----- handlers --------------------------------------------------------

    async def _complete(self, system: str, prompt: str, run_model: str,
                        echo_model: str) -> JSONResponse:
        cid, created = _completion_id(), _now()
        text, final = "", None
        async for ev in self.engine.openai_complete(system, prompt, run_model):
            if ev["type"] == "delta":
                text += ev["text"]
            elif ev["type"] == "error":
                return _error(ev["message"], "server_error", 500)
            elif ev["type"] == "final":
                final = ev
        if final is None:
            return _error("No completion produced.", "server_error", 500)
        return JSONResponse(completion_response(
            cid, created, echo_model, final["text"] or text,
            final["finish_reason"], final["usage"]))

    async def _stream(self, system: str, prompt: str, run_model: str,
                      echo_model: str, include_usage: bool):
        cid, created = _completion_id(), _now()
        yield _sse(_chunk(cid, created, echo_model, {"role": "assistant", "content": ""}))
        final = None
        try:
            async for ev in self.engine.openai_complete(system, prompt, run_model):
                if ev["type"] == "delta":
                    yield _sse(_chunk(cid, created, echo_model, {"content": ev["text"]}))
                elif ev["type"] == "error":
                    yield _sse({"error": {"message": ev["message"],
                                          "type": "server_error", "param": None, "code": None}})
                    yield "data: [DONE]\n\n"
                    return
                elif ev["type"] == "final":
                    final = ev
        except Exception as exc:
            yield _sse({"error": {"message": f"{type(exc).__name__}: {exc}",
                                  "type": "server_error", "param": None, "code": None}})
            yield "data: [DONE]\n\n"
            return
        yield _sse(_chunk(cid, created, echo_model, {},
                          final["finish_reason"] if final else "stop"))
        if include_usage:
            usage = final["usage"] if final else {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            yield _sse(_usage_chunk(cid, created, echo_model, usage))
        yield "data: [DONE]\n\n"

    # ----- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        cfg = self.config.openai_server
        uconf = uvicorn.Config(self.app, host=cfg.host, port=cfg.port,
                               log_level="warning")
        self._server = uvicorn.Server(uconf)
        import asyncio
        self._serve_task = asyncio.create_task(self._server.serve())
        self.log.info("OpenAI endpoint on http://%s:%d/v1", cfg.host, cfg.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            import asyncio
            try:
                await asyncio.wait_for(self._serve_task, timeout=10)
            except (asyncio.TimeoutError, Exception):
                pass
