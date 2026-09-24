"""信息线层：SQLite 事件真值 + FTS5，FTS 不可用时回退参数化 LIKE。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from shared.redact import redact

from core.library.models import EventStatus, EventType, LibraryEvent
from core.library.paths import ensure_regular_data_file

MAX_EVENT_TEXT = 100_000
_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+")
_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "raw": {"indexed", "linked", "failed", "skipped"},
    "failed": {"raw", "skipped"},
    "skipped": {"raw"},
    "indexed": {"linked", "failed", "skipped"},
    "linked": {"indexed", "failed", "skipped"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def content_hash(text: str) -> str:
    return hashlib.blake2s(text.encode("utf-8", errors="replace"), digest_size=16).hexdigest()


def query_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(token.casefold() for token in _TOKEN.findall(query or "") if token))[:32]


class LineStore:
    """短连接、单写者存储；连接不跨请求保留，禁用 DPIM 后可完整释放。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = ensure_regular_data_file(self.root, "memory.db")
        self._fts_available: bool | None = None

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = ensure_regular_data_file(self.root, "memory.db")
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        self._ensure_schema(connection)
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                raw_content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('interaction','data','source')),
                status TEXT NOT NULL CHECK(status IN ('raw','indexed','linked','failed','skipped')),
                graph_refs TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS ix_events_created ON events(created_at DESC)")
        if self._fts_available is False:
            return
        try:
            had_fts = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts'"
            ).fetchone() is not None
            had_trigger = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='events_fts_insert'"
            ).fetchone() is not None
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(raw_content, content='events', content_rowid='rowid')"
            )
            connection.execute(
                """CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
                    INSERT INTO events_fts(rowid, raw_content) VALUES (new.rowid, new.raw_content);
                END"""
            )
            # Rebuild only when introducing FTS/its trigger, never on every short connection.
            if not had_fts or not had_trigger:
                connection.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
            self._fts_available = True
        except sqlite3.DatabaseError:
            self._fts_available = False

    @property
    def fts_available(self) -> bool:
        if self._fts_available is not None:
            return self._fts_available
        with closing(self._connect()) as connection:
            connection.commit()
        return bool(self._fts_available)

    def append(self, event_id: str, text: str, event_type: EventType) -> LibraryEvent:
        body = str(redact(str(text or "")) or "")
        if not body.strip():
            raise ValueError("事件内容不能为空。")
        if len(body) > MAX_EVENT_TEXT:
            raise ValueError(f"单条事件不能超过 {MAX_EVENT_TEXT} 个字符。")
        event = LibraryEvent(
            event_id=event_id,
            created_at=_now(),
            raw_content=body,
            content_hash=content_hash(body),
            event_type=event_type,
        )
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """INSERT INTO events(event_id,created_at,raw_content,content_hash,event_type,status)
                       VALUES(?,?,?,?,?,'raw')""",
                    (event.event_id, event.created_at, event.raw_content, event.content_hash, event.event_type),
                )
        return event

    def get(self, event_id: str) -> LibraryEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def transition(
        self, event_id: str, status: EventStatus, *, graph_refs: list[str] | None = None, error: str = ""
    ) -> None:
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute("SELECT status FROM events WHERE event_id=?", (event_id,)).fetchone()
                if row is None:
                    raise KeyError(f"事件不存在：{event_id}")
                old = str(row["status"])
                if status != old and status not in _STATUS_TRANSITIONS.get(old, set()):
                    raise ValueError(f"非法事件状态转换：{old} → {status}")
                if graph_refs is None:
                    connection.execute(
                        "UPDATE events SET status=?, error=? WHERE event_id=?",
                        (status, str(error)[:500], event_id),
                    )
                else:
                    refs = list(dict.fromkeys(str(ref) for ref in graph_refs if ref))[:500]
                    connection.execute(
                        "UPDATE events SET status=?, graph_refs=?, error=? WHERE event_id=?",
                        (status, json.dumps(refs, ensure_ascii=False), str(error)[:500], event_id),
                    )

    def set_graph_refs(self, refs_by_event: dict[str, list[str]]) -> None:
        """由 graph.reconcile 回写派生反向索引，不触碰事件原文。"""
        with closing(self._connect()) as connection:
            with connection:
                for event_id, refs in refs_by_event.items():
                    normalized = list(dict.fromkeys(refs))[:500]
                    row = connection.execute(
                        "SELECT status FROM events WHERE event_id=?", (event_id,)
                    ).fetchone()
                    if row is None:
                        continue
                    status = "linked" if normalized else ("indexed" if row["status"] == "linked" else row["status"])
                    connection.execute(
                        "UPDATE events SET graph_refs=?,status=? WHERE event_id=?",
                        (json.dumps(normalized, ensure_ascii=False), status, event_id),
                    )

    def page(self, offset: int = 0, limit: int = 50) -> list[LibraryEvent]:
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("事件分页参数超出范围。")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY created_at DESC,event_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._event(row) for row in rows]

    def offset_of(self, event_id: str) -> int | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT created_at,event_id FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            count = connection.execute(
                """SELECT COUNT(*) FROM events
                   WHERE created_at > ? OR (created_at = ? AND event_id > ?)""",
                (row["created_at"], row["created_at"], row["event_id"]),
            ).fetchone()[0]
        return int(count)

    def search(self, query: str, limit: int = 50) -> list[LibraryEvent]:
        tokens = query_tokens(query)
        if not tokens:
            return []
        limit = max(1, min(int(limit), 200))
        found: list[sqlite3.Row] = []
        with closing(self._connect()) as connection:
            if self._fts_available:
                match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
                try:
                    found = connection.execute(
                        """SELECT e.* FROM events_fts f JOIN events e ON e.rowid=f.rowid
                           WHERE events_fts MATCH ? AND e.status != 'skipped' ORDER BY rank LIMIT ?""",
                        (match, limit),
                    ).fetchall()
                except sqlite3.DatabaseError:
                    self._fts_available = False
            if not found:
                patterns = [
                    f"%{token.replace('!', '!!').replace('%', '!%').replace('_', '!_')}%"
                    for token in tokens
                ]
                condition = " OR ".join("raw_content LIKE ? ESCAPE '!'" for _ in patterns)
                found = connection.execute(
                    f"SELECT * FROM events WHERE status != 'skipped' AND ({condition}) ORDER BY created_at DESC LIMIT ?",
                    (*patterns, limit),
                ).fetchall()
        return [self._event(row) for row in found]

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
            total = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"total": int(total), **{str(row["status"]): int(row["n"]) for row in rows}}

    def all_ids(self) -> set[str]:
        with closing(self._connect()) as connection:
            return {str(row[0]) for row in connection.execute("SELECT event_id FROM events")}

    @staticmethod
    def _event(row: sqlite3.Row) -> LibraryEvent:
        try:
            refs = json.loads(row["graph_refs"] or "[]")
        except (ValueError, TypeError):
            refs = []
        return LibraryEvent(
            event_id=row["event_id"],
            created_at=row["created_at"],
            raw_content=row["raw_content"],
            content_hash=row["content_hash"],
            event_type=row["event_type"],
            status=row["status"],
            graph_refs=refs if isinstance(refs, list) else [],
            error=row["error"] or "",
        )
