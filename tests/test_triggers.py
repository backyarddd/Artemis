"""Scheduler, webhook HMAC + routing, and the goal loop tick."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from artemis.config import GlobalConfig
from artemis.triggers.goal_loop import GoalLoop, parse_goals
from artemis.triggers.scheduler import Scheduler
from artemis.triggers.webhooks import WebhookServer, verify_hmac


async def _noop(task):
    return task.id


def test_scheduler_add_list_remove(profile):
    sch = Scheduler(profile, GlobalConfig(), _noop)
    jid = sch.add_job("*/5 * * * *", prompt="ping")
    assert any(j["id"] == jid for j in sch.list_jobs())
    assert sch.remove_job(jid) is True
    assert not any(j["id"] == jid for j in sch.list_jobs())


def test_scheduler_rejects_bad_cron(profile):
    sch = Scheduler(profile, GlobalConfig(), _noop)
    with pytest.raises(Exception):
        sch.add_job("not a cron", prompt="x")


def test_parse_goals():
    text = "- do the thing\n- # a comment\n-   \n- another goal\nplain line"
    assert parse_goals(text) == ["do the thing", "another goal"]


def test_verify_hmac():
    sig = hmac.new(b"sek", b"body", hashlib.sha256).hexdigest()
    assert verify_hmac("sek", b"body", sig)
    assert not verify_hmac("sek", b"body", "deadbeef")
    gh = "sha256=" + hmac.new(b"sek", b"body", hashlib.sha256).hexdigest()
    assert verify_hmac("sek", b"body", gh, prefix="sha256=")


def test_webhook_generic_enqueues(profile, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("ARTEMIS_WEBHOOK_SECRET", "topsecret")
    queued = []

    async def enqueue(task):
        queued.append(task)
        return task.id

    server = WebhookServer(profile, GlobalConfig(), enqueue)
    client = TestClient(server.app)

    assert client.get("/health").json() == {"ok": True}

    body = b'{"prompt": "from webhook"}'
    sig = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook/deploy", content=body,
                    headers={"X-Artemis-Signature": sig})
    assert r.status_code == 200
    assert queued and queued[0].prompt == "from webhook"

    bad = client.post("/webhook/deploy", content=body,
                      headers={"X-Artemis-Signature": "wrong"})
    assert bad.status_code == 401


def test_webhook_requires_secret(profile, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("ARTEMIS_WEBHOOK_SECRET", raising=False)
    server = WebhookServer(profile, GlobalConfig(), _noop)
    client = TestClient(server.app)
    r = client.post("/webhook/x", content=b"{}", headers={"X-Artemis-Signature": "z"})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_goal_loop_tick_enqueues(profile):
    profile.goals_md.write_text("- summarize the inbox\n- check the deploy\n")
    cfg = GlobalConfig()
    cfg.goal_loop.enabled = True
    queued = []

    async def enqueue(task):
        queued.append(task)
        return task.id

    loop = GoalLoop(profile, cfg, enqueue)
    await loop._tick()
    assert queued and "summarize the inbox" in queued[0].prompt


@pytest.mark.asyncio
async def test_goal_loop_respects_pause(profile):
    from artemis.approval import ProfileState
    profile.goals_md.write_text("- do work\n")
    st = ProfileState.load(profile)
    st.paused = True
    st.save()
    cfg = GlobalConfig()
    cfg.goal_loop.enabled = True
    queued = []

    async def enqueue(task):
        queued.append(task)
        return task.id

    loop = GoalLoop(profile, cfg, enqueue)
    await loop._tick()
    assert queued == []
