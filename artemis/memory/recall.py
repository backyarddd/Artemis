"""Recall: turn stored facts into a compact system-prompt block.

The returned string is fed to EngineRunSpec.system_prompt_append so a fresh run
carries forward durable context without a full session resume.
"""

from __future__ import annotations

from typing import Optional

from .store import MemoryStore

_HEADER = "Relevant prior context:"
_MAX_FACT_CHARS = 240


def recall(store: MemoryStore, query: str, limit: int = 5,
           profile: Optional[str] = None) -> str:
    """Search the store and format a bullet block, or "" if nothing matches."""
    try:
        hits = store.search(query, limit=limit, profile=profile)
    except Exception:
        return ""
    lines = []
    for h in hits:
        text = (h.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        if len(text) > _MAX_FACT_CHARS:
            text = text[:_MAX_FACT_CHARS] + "..."
        lines.append(f"- {text}")
    if not lines:
        return ""
    return _HEADER + "\n" + "\n".join(lines)


def build_query_from_task(task) -> str:
    """Derive a short search query from a Task (prompt or command_ref/args)."""
    prompt = (getattr(task, "prompt", None) or "").strip()
    if prompt:
        return prompt[:200]
    ref = getattr(task, "command_ref", None)
    if ref:
        args = getattr(task, "command_args", None) or {}
        arg_text = " ".join(str(v) for v in args.values())
        return f"{ref} {arg_text}".strip()[:200]
    return ""
