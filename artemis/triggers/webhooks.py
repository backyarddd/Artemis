"""Webhook trigger: a FastAPI app served by uvicorn, HMAC-verified.

Generic ``POST /webhook/{name}`` accepts a JSON Task spec; ``POST /webhook/github``
parses a GitHub push and summarizes it. Both verify HMAC-SHA256 against the
profile's ``webhook_secret`` (env/keyring). No secret configured means the
endpoint rejects with 503. start() launches uvicorn in a background task.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any, Awaitable, Callable, Optional

import uvicorn
from fastapi import FastAPI, Request, Response

from ..config import GlobalConfig, get_secret
from ..models import ApprovalMode, ChannelTarget, Task, TaskSource
from ..observability import get_logger
from ..profiles import Profile

Enqueue = Callable[[Task], Awaitable[str]]


def verify_hmac(secret: str, body: bytes, signature: str, prefix: str = "") -> bool:
    """Constant-time HMAC-SHA256 check. ``prefix`` strips e.g. "sha256=" first."""
    if not secret or not signature:
        return False
    if prefix and signature.startswith(prefix):
        signature = signature[len(prefix):]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


class WebhookServer:
    """Serves the webhook FastAPI app; each valid hit enqueues a WEBHOOK Task."""

    def __init__(self, profile: Profile, config: GlobalConfig, enqueue: Enqueue):
        self.profile = profile
        self.config = config
        self.enqueue = enqueue
        self.log = get_logger("artemis.webhooks", profile.logs_dir, config.log_level)
        self.app = self._build_app()
        self._server: Optional[uvicorn.Server] = None
        self._serve_task: Optional[asyncio.Task] = None

    # ----- secret ----------------------------------------------------------

    def _secret(self) -> Optional[str]:
        return get_secret("webhook_secret", self.profile.name)

    # ----- app -------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Artemis webhooks")

        @app.get("/health")
        async def health() -> dict[str, bool]:
            return {"ok": True}

        @app.post("/webhook/github")
        async def github(request: Request) -> Response:
            secret = self._secret()
            if not secret:
                return Response("webhook secret not configured", status_code=503)
            body = await request.body()
            sig = request.headers.get("X-Hub-Signature-256", "")
            if not verify_hmac(secret, body, sig, prefix="sha256="):
                return Response("bad signature", status_code=401)
            payload = _parse_json(body)
            prompt = _summarize_github_push(payload)
            task = Task(prompt=prompt, profile=self.profile.name,
                        source=TaskSource.WEBHOOK)
            tid = await self.enqueue(task)
            return _json({"queued": tid})

        @app.post("/webhook/{name}")
        async def generic(name: str, request: Request) -> Response:
            secret = self._secret()
            if not secret:
                return Response("webhook secret not configured", status_code=503)
            body = await request.body()
            sig = request.headers.get("X-Artemis-Signature", "")
            if not verify_hmac(secret, body, sig):
                return Response("bad signature", status_code=401)
            spec = _parse_json(body)
            task = self._task_from_spec(name, spec)
            tid = await self.enqueue(task)
            return _json({"queued": tid})

        return app

    def _task_from_spec(self, name: str, spec: dict[str, Any]) -> Task:
        channel = spec.get("channel")
        conv = spec.get("conversation_id")
        target = ChannelTarget(channel, conv) if channel and conv else None
        mode = (ApprovalMode.coerce(spec["approval_mode"])
                if spec.get("approval_mode") else None)
        prompt = spec.get("prompt")
        command_ref = spec.get("command_ref")
        if not prompt and not command_ref:
            prompt = f"Webhook '{name}' fired with payload: {json.dumps(spec)[:500]}"
        return Task(
            prompt=prompt,
            command_ref=command_ref,
            command_args=spec.get("command_args") or {},
            profile=self.profile.name,
            source=TaskSource.WEBHOOK,
            channel_target=target,
            approval_mode_override=mode,
        )

    # ----- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        config_u = uvicorn.Config(self.app, host=self.config.webhook.host,
                                  port=self.config.webhook.port,
                                  log_level="warning")
        self._server = uvicorn.Server(config_u)
        self._serve_task = asyncio.create_task(self._server.serve())
        self.log.info("webhook server listening on %s:%d",
                      self.config.webhook.host, self.config.webhook.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
            self._serve_task = None
        self._server = None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _parse_json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body or b"{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _json(payload: dict[str, Any]) -> Response:
    return Response(json.dumps(payload), media_type="application/json")


def _summarize_github_push(payload: dict[str, Any]) -> str:
    repo = (payload.get("repository") or {}).get("full_name", "unknown repo")
    pusher = (payload.get("pusher") or {}).get("name", "someone")
    head = payload.get("head_commit") or {}
    msg = head.get("message", "").strip().replace("\n", " ")
    ref = payload.get("ref", "")
    parts = [f"GitHub push to {repo} by {pusher}"]
    if ref:
        parts.append(f"on {ref}")
    if msg:
        parts.append(f'- latest commit: "{msg[:120]}"')
    summary = " ".join(parts)
    return (f"A push landed on a watched repository. Review it and take any "
            f"warranted action, then report.\n\n{summary}")
