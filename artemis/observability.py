"""Structured logging, per-task audit trail, and cost tracking.

Audit and cost are file-backed (jsonl/json under the profile's logs dir) so they
work before the sqlite store exists and survive restarts. Every tool decision,
manual or automatic, is recorded here.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

_LOGGERS: dict[str, logging.Logger] = {}


def get_logger(name: str = "artemis", logs_dir: Optional[Path] = None,
               level: str = "INFO") -> logging.Logger:
    key = f"{name}:{logs_dir}"
    if key in _LOGGERS:
        return _LOGGERS[key]
    logger = logging.getLogger(name if logs_dir is None else f"{name}.{logs_dir}")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logs_dir / "artemis.log")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    _LOGGERS[key] = logger
    return logger


def _excerpt(value: Any, limit: int = 300) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        s = str(value)
    return s if len(s) <= limit else s[:limit] + "..."


class AuditLog:
    """Append-only jsonl audit trail. One file per profile."""

    def __init__(self, logs_dir: Path):
        self.path = logs_dir / "audit.jsonl"
        self._lock = threading.Lock()
        logs_dir.mkdir(parents=True, exist_ok=True)

    def _write(self, record: dict) -> None:
        record["ts"] = time.time()
        line = json.dumps(record, default=str)
        with self._lock:
            with open(self.path, "a") as f:
                f.write(line + "\n")

    def tool_decision(self, task_id: str, tool_name: str, decision: str,
                      mode: str, reason: str = "", tool_input: Any = None,
                      automatic: bool = True) -> None:
        self._write({
            "type": "tool_decision",
            "task_id": task_id,
            "tool": tool_name,
            "decision": decision,
            "mode": mode,
            "automatic": automatic,
            "reason": reason,
            "input": _excerpt(tool_input),
        })

    def task_event(self, task_id: str, event: str, **fields: Any) -> None:
        rec = {"type": "task_event", "task_id": task_id, "event": event}
        rec.update({k: _excerpt(v) for k, v in fields.items()})
        self._write(rec)

    def hook_block(self, task_id: str, tool_name: str, reason: str,
                   tool_input: Any = None) -> None:
        self._write({
            "type": "hook_block",
            "task_id": task_id,
            "tool": tool_name,
            "reason": reason,
            "input": _excerpt(tool_input),
        })

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out


class CostTracker:
    """Accumulates spend per task and per day for budget enforcement and /status."""

    def __init__(self, logs_dir: Path):
        self.path = logs_dir / "cost.json"
        self._lock = threading.Lock()
        logs_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"days": {}, "tasks": {}}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {"days": {}, "tasks": {}}

    def add(self, task_id: str, cost_usd: float) -> None:
        if not cost_usd:
            return
        today = date.today().isoformat()
        with self._lock:
            data = self._load()
            data["days"][today] = round(data["days"].get(today, 0.0) + cost_usd, 6)
            data["tasks"][task_id] = round(data["tasks"].get(task_id, 0.0) + cost_usd, 6)
            self.path.write_text(json.dumps(data))

    def today_total(self) -> float:
        return self._load()["days"].get(date.today().isoformat(), 0.0)

    def task_total(self, task_id: str) -> float:
        return self._load()["tasks"].get(task_id, 0.0)

    def summary(self) -> dict:
        data = self._load()
        return {
            "today": data["days"].get(date.today().isoformat(), 0.0),
            "all_time": round(sum(data["days"].values()), 6),
            "tasks_tracked": len(data["tasks"]),
        }
