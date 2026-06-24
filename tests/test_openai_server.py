"""OpenAI-compatible endpoint: wire-format compliance with a mocked engine
(no API calls). Covers models, non-streaming, streaming SSE, auth, errors."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from artemis.config import GlobalConfig
from artemis.server.openai_api import OpenAIServer


def _server(profile, require_auth=False):
    cfg = GlobalConfig()
    cfg.openai_server.require_auth = require_auth
    server = OpenAIServer(profile, cfg)

    async def fake_complete(system, prompt, model):
        for tok in ["Hello", ", ", "world"]:
            yield {"type": "delta", "text": tok}
        yield {"type": "final", "finish_reason": "stop",
               "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
               "text": "Hello, world"}

    server.engine.openai_complete = fake_complete
    return server


def test_models(profile):
    client = TestClient(_server(profile).app)
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"] and all(m["object"] == "model" for m in body["data"])
    assert all("id" in m and "created" in m and "owned_by" in m for m in body["data"])
    # non-/v1 fallback also works
    assert client.get("/models").status_code == 200


def test_non_streaming(profile):
    client = TestClient(_server(profile).app)
    r = client.post("/v1/chat/completions", json={
        "model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert body["model"] == "gpt-4o"  # echoes requested model
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"] == {"role": "assistant", "content": "Hello, world"}
    assert choice["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}


def test_permissive_unknown_fields_and_headers(profile):
    client = TestClient(_server(profile).app)
    r = client.post("/v1/chat/completions",
                    headers={"X-Stainless-Lang": "python", "OpenAI-Organization": "org"},
                    json={"model": "m", "messages": [{"role": "user", "content": "hi"}],
                          "temperature": 0.5, "tools": [], "frobnicate": True, "seed": 7})
    assert r.status_code == 200


def test_content_parts_array(profile):
    client = TestClient(_server(profile).app)
    r = client.post("/v1/chat/completions", json={
        "model": "m", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "http://x"}}]}]})
    assert r.status_code == 200


def test_missing_messages_400(profile):
    client = TestClient(_server(profile).app)
    r = client.post("/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert r.json()["error"]["param"] == "messages"


def test_streaming_format(profile):
    client = TestClient(_server(profile).app)
    with client.stream("POST", "/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "stream": True}) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        events = []
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(line[6:])
    assert events[-1] == "[DONE]"
    parsed = [json.loads(e) for e in events[:-1]]
    assert all(p["object"] == "chat.completion.chunk" for p in parsed)
    assert parsed[0]["choices"][0]["delta"]["role"] == "assistant"
    contents = "".join(p["choices"][0]["delta"].get("content", "")
                       for p in parsed if p["choices"])
    assert contents == "Hello, world"
    # final content chunk carries finish_reason
    assert parsed[-1]["choices"][0]["finish_reason"] == "stop"
    # constant id across chunks
    assert len({p["id"] for p in parsed}) == 1


def test_streaming_usage_chunk(profile):
    client = TestClient(_server(profile).app)
    with client.stream("POST", "/v1/chat/completions", json={
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "stream": True, "stream_options": {"include_usage": True}}) as r:
        events = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
    parsed = [json.loads(e) for e in events if e != "[DONE]"]
    usage_chunks = [p for p in parsed if p.get("usage") and not p["choices"]]
    assert usage_chunks and usage_chunks[0]["usage"]["total_tokens"] == 8


def test_auth_enforced(profile, monkeypatch):
    # Provide the key via env (get_secret checks env before keyring) so the test
    # never touches the real OS keychain.
    monkeypatch.setenv("ARTEMIS_OPENAI_API_KEY", "sk-test-123")
    client = TestClient(_server(profile, require_auth=True).app)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat/completions", json=body).status_code == 401
    assert client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.post("/v1/chat/completions", json=body,
                     headers={"Authorization": "Bearer sk-test-123"})
    assert ok.status_code == 200
