"""Extract durable facts from a completed task and persist them.

With an injected llm callable, facts are distilled by a cheap isolated query and
parsed robustly. Without one, a single heuristic summary fact is stored. This
never raises: extraction failures must not break a run.
"""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Optional

from ..observability import get_logger
from .store import MemoryStore

_log = get_logger("artemis.memory")

LLMCallable = Callable[[str, str], Awaitable[str]]

_MAX_FACTS = 5
_HEURISTIC_CHARS = 200

_EXTRACT_SYSTEM = (
    "You distill durable, reusable facts from a finished agent task. "
    "Return ONLY a JSON array of 0 to 5 short strings. Each string is a fact "
    "worth remembering across future sessions (decisions, locations, conventions, "
    "credentials-free configuration, learned constraints). Skip transient chatter, "
    "greetings, and anything tied only to this one run. No prose, just the array."
)


async def extract_and_store(store: MemoryStore, profile_name: str, task,
                            result, llm: Optional[LLMCallable] = None) -> int:
    """Extract 0-5 facts from a completed task and store them. Returns count."""
    try:
        if getattr(result, "is_error", False):
            return 0
        final_text = (getattr(result, "final_text", "") or "").strip()
        if llm is not None:
            return await _extract_with_llm(store, profile_name, task, final_text, llm)
        return _extract_heuristic(store, profile_name, task, final_text)
    except Exception:
        _log.exception("extract_and_store failed")
        return 0


async def _extract_with_llm(store: MemoryStore, profile_name: str, task,
                            final_text: str, llm: LLMCallable) -> int:
    prompt = (getattr(task, "prompt", None) or getattr(task, "command_ref", None) or "").strip()
    user = f"Task:\n{prompt[:1000]}\n\nResult:\n{final_text[:2000]}"
    raw = await llm(_EXTRACT_SYSTEM, user)
    facts = _parse_facts(raw)
    count = 0
    for fact in facts[:_MAX_FACTS]:
        if store.add_fact(fact, kind="fact",
                          source_task=getattr(task, "id", None),
                          profile=profile_name):
            count += 1
    return count


def _extract_heuristic(store: MemoryStore, profile_name: str, task,
                       final_text: str) -> int:
    if not final_text:
        return 0
    summary = final_text.replace("\n", " ").strip()[:_HEURISTIC_CHARS]
    desc = task.describe() if hasattr(task, "describe") else ""
    text = f"Task '{desc}': {summary}" if desc else summary
    return 1 if store.add_fact(text, kind="fact",
                               source_task=getattr(task, "id", None),
                               profile=profile_name) else 0


def _parse_facts(raw: str) -> list[str]:
    """Robustly pull a JSON array of fact strings out of llm output."""
    if not raw:
        return []
    text = raw.strip()
    # Strip markdown code fences if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Isolate the first bracketed array, tolerating leading/trailing prose.
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out
