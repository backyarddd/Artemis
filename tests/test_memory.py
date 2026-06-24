"""Memory store (FTS5/LIKE), recall formatting, session persistence, and
heuristic fact extraction (no LLM)."""

from __future__ import annotations

import pytest

from artemis.memory.recall import build_query_from_task, recall
from artemis.memory.store import MemoryStore
from artemis.models import RunResult, Task


def test_add_search_recent(profile):
    store = MemoryStore(profile.db_path)
    store.add_fact("the deploy script lives in scripts/deploy.sh", profile="default")
    store.add_fact("staging uses postgres 16", profile="default")
    hits = store.search("deploy", profile="default")
    assert hits and any("deploy.sh" in h["text"] for h in hits)
    assert len(store.recent(profile="default")) == 2
    store.close()


def test_search_tolerates_punctuation(profile):
    store = MemoryStore(profile.db_path)
    store.add_fact("api key rotates monthly", profile="default")
    # Must not raise on FTS5 special characters.
    assert isinstance(store.search('api: "key" (rotate)*', profile="default"), list)
    store.close()


def test_recall_block(profile):
    store = MemoryStore(profile.db_path)
    store.add_fact("the deploy script lives in scripts/deploy.sh", profile="default")
    block = recall(store, "deploy", profile="default")
    assert "deploy.sh" in block
    assert recall(store, "nonexistent topic zzz", profile="default") == ""
    store.close()


def test_session_persistence(profile):
    store = MemoryStore(profile.db_path)
    store.save_session("task1", "sess-abc", project="telegram:42", profile="default")
    got = store.get_session(project="telegram:42", profile="default")
    assert got and got["session_id"] == "sess-abc"
    store.close()


def test_query_from_task():
    t = Task(prompt="summarize the quarterly revenue report")
    q = build_query_from_task(t)
    assert isinstance(q, str) and q


@pytest.mark.asyncio
async def test_extract_heuristic(profile):
    from artemis.memory.extract import extract_and_store
    store = MemoryStore(profile.db_path)
    task = Task(prompt="investigate the flaky test", profile="default")
    result = RunResult(final_text="The flaky test was caused by a race in setup.", is_error=False)
    n = await extract_and_store(store, "default", task, result, llm=None)
    assert n >= 1
    assert store.search("flaky", profile="default")
    store.close()
