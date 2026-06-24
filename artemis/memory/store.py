"""SQLite-backed memory store with FTS5 full-text search.

Persists durable facts and session-resume records per profile. FTS5 is used when
the sqlite build supports it; otherwise search degrades to LIKE. All writes are
guarded by a lock so the store is safe to share across threads (callers may wrap
sync methods in asyncio.to_thread).
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from ..observability import get_logger

# Characters that carry meaning inside an FTS5 MATCH expression. We strip them
# from raw user queries before building a safe MATCH string.
_FTS_SPECIAL = re.compile(r'[^\w\s]')


class MemoryStore:
    """Per-profile fact + session store. Sync API, thread-safe writes."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.log = get_logger("artemis.memory")
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.fts = False
        self._init_schema()

    # ----- schema ----------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS facts(
                    id INTEGER PRIMARY KEY,
                    profile TEXT,
                    ts REAL,
                    source_task TEXT,
                    kind TEXT,
                    text TEXT,
                    tags TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions(
                    task_id TEXT,
                    profile TEXT,
                    session_id TEXT,
                    project TEXT,
                    summary TEXT,
                    ts REAL
                )
            """)
            self._conn.commit()
            self._init_fts(cur)

    def _init_fts(self, cur: sqlite3.Cursor) -> None:
        """Create the FTS5 mirror + triggers. Fall back to LIKE on failure."""
        try:
            cur.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                USING fts5(text, content='facts', content_rowid='id')
            """)
            # Keep the external-content FTS index in sync with the facts table.
            cur.execute("""
                CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
                END
            """)
            cur.execute("""
                CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, text)
                    VALUES ('delete', old.id, old.text);
                END
            """)
            cur.execute("""
                CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, text)
                    VALUES ('delete', old.id, old.text);
                    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
                END
            """)
            self._conn.commit()
            self.fts = True
        except sqlite3.Error as exc:
            self.fts = False
            self.log.warning("FTS5 unavailable, using LIKE search: %s", exc)

    # ----- facts -----------------------------------------------------------

    def add_fact(self, text: str, kind: str = "fact",
                 source_task: Optional[str] = None,
                 tags: Optional[str] = None,
                 profile: Optional[str] = None) -> int:
        text = (text or "").strip()
        if not text:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO facts(profile, ts, source_task, kind, text, tags) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (profile, time.time(), source_task, kind, text, tags),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def search(self, query: str, limit: int = 5,
               profile: Optional[str] = None) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        if self.fts:
            match = self._build_match(query)
            if match:
                try:
                    return self._search_fts(match, limit, profile)
                except sqlite3.Error as exc:
                    self.log.warning("FTS query failed, falling back to LIKE: %s", exc)
        return self._search_like(query, limit, profile)

    def _build_match(self, query: str) -> str:
        """Turn arbitrary text into a safe FTS5 MATCH string.

        Strip special chars, keep word tokens, quote each so punctuation in the
        original never reaches the FTS5 parser. OR the terms for recall breadth.
        """
        cleaned = _FTS_SPECIAL.sub(" ", query)
        terms = [t for t in cleaned.split() if t]
        if not terms:
            return ""
        return " OR ".join(f'"{t}"' for t in terms)

    def _search_fts(self, match: str, limit: int,
                    profile: Optional[str]) -> list[dict]:
        cur = self._conn.cursor()
        if profile is not None:
            rows = cur.execute(
                "SELECT f.* FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                "WHERE facts_fts MATCH ? AND f.profile = ? "
                "ORDER BY rank LIMIT ?",
                (match, profile, limit),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT f.* FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def _search_like(self, query: str, limit: int,
                     profile: Optional[str]) -> list[dict]:
        cur = self._conn.cursor()
        pattern = f"%{query}%"
        if profile is not None:
            rows = cur.execute(
                "SELECT * FROM facts WHERE text LIKE ? AND profile = ? "
                "ORDER BY ts DESC LIMIT ?",
                (pattern, profile, limit),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT * FROM facts WHERE text LIKE ? ORDER BY ts DESC LIMIT ?",
                (pattern, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 10,
               profile: Optional[str] = None) -> list[dict]:
        cur = self._conn.cursor()
        if profile is not None:
            rows = cur.execute(
                "SELECT * FROM facts WHERE profile = ? ORDER BY ts DESC LIMIT ?",
                (profile, limit),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT * FROM facts ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- sessions --------------------------------------------------------

    def save_session(self, task_id: str, session_id: str,
                     project: Optional[str] = None,
                     summary: Optional[str] = None,
                     profile: Optional[str] = None) -> None:
        """Upsert a resume record keyed by task_id."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM sessions WHERE task_id = ?", (task_id,))
            cur.execute(
                "INSERT INTO sessions(task_id, profile, session_id, project, summary, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, profile, session_id, project, summary, time.time()),
            )
            self._conn.commit()

    def get_session(self, task_id: Optional[str] = None,
                    project: Optional[str] = None,
                    profile: Optional[str] = None) -> Optional[dict]:
        """Latest session matching the given filters, for resume."""
        clauses = []
        params: list = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if profile is not None:
            clauses.append("profile = ?")
            params.append(profile)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.cursor()
        row = cur.execute(
            f"SELECT * FROM sessions{where} ORDER BY ts DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        return dict(row) if row else None

    # ----- lifecycle -------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
