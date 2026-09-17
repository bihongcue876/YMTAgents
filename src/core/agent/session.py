"""会话存储（spec §2.5 / docs 03 §4）。

- 会话自足：回放只需自身目录，不依赖任何配置当前值（快照原则）。
- events.jsonl 是唯一事实源；append-only。
- 删除只移除索引/引用，历史目录保留至用户手动清理。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shared.envelope import SessionMeta
from shared.ids import SESS, new_id

from core.bus.sink import EventSink
from core.store.atomic import atomic_write_json


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionSnapshot:
    meta: SessionMeta
    events: list[dict] = field(default_factory=list)


def wire_to_line(event) -> tuple[str, str, dict] | None:
    """把 wire 事件映射为 03 §6 落盘行 (src, type, payload)；不可落盘返回 None。"""
    t = getattr(event, "type", None)
    d = event.model_dump(mode="json")
    if t == "msg.user":
        return ("user", "msg.user", {"text": d["text"], "attachments": d["attachments"]})
    if t == "msg.assistant.delta":
        return ("agent", "msg.assistant.delta", {"content": d["content"], "turn_seq": d["turn_seq"]})
    if t == "msg.assistant.final":
        return (
            "agent",
            "msg.assistant.final",
            {
                "content": d["content"],
                "turn_seq": d["turn_seq"],
                "usage": d["usage"],
                "interrupted": d["interrupted"],
                "truncated": d["truncated"],
            },
        )
    if t == "ctx.usage":
        return ("agent", "ctx.usage", {"segments": d["segments"], "total": d["total"], "window": d["window"]})
    if t == "error":
        return (
            "agent",
            "error",
            {"scope": d["scope"], "code": d["code"], "message": d["message"], "detail": d.get("detail")},
        )
    return None


class ISessionStore(ABC):
    @abstractmethod
    def create(self, title: str | None, persona_id: str | None) -> SessionMeta: ...

    @abstractmethod
    def resume(self, session_id: str) -> SessionSnapshot: ...

    @abstractmethod
    def list(self, include_archived: bool = False) -> list[SessionMeta]: ...

    @abstractmethod
    def archive(self, session_id: str) -> None: ...

    @abstractmethod
    def unarchive(self, session_id: str) -> SessionMeta:
        """从归档恢复（spec rev3 `session.unarchive`）。"""

    @abstractmethod
    def rename(self, session_id: str, title: str) -> SessionMeta:
        """就地改名（spec rev3 `session.rename`）。"""

    @abstractmethod
    def set_model(self, session_id: str, model_id: str | None, slot: str = "main") -> SessionMeta:
        """会话级模型切换，落 `model.switch` 事件（docs 03 §3.1）。"""

    @abstractmethod
    def delete(self, session_id: str) -> None: ...

    @abstractmethod
    def end(self, session_id: str, reason: str) -> None:
        """收尾会话：落 `session.end` 并把事件流 fsync 到磁盘（docs 03 §10）。"""

    @abstractmethod
    def append_event(self, session_id: str, event) -> None: ...

    @abstractmethod
    def replay(self, session_id: str) -> list[dict]: ...


class SessionStore(ISessionStore):
    def __init__(self, root: Path, sink: EventSink | None = None) -> None:
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.sink = sink or EventSink(self.root)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            atomic_write_json(self.index_path, [])

    # -- 索引 --------------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.sessions_dir / "index.json"

    def _load_index(self) -> list[dict]:
        import json

        if not self.index_path.exists():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_index(self, items: list[dict]) -> None:
        atomic_write_json(self.index_path, items)

    def _upsert_index(self, meta: SessionMeta) -> None:
        items = self._load_index()
        items = [i for i in items if i.get("id") != meta.id]
        items.append(meta.model_dump(mode="json"))
        self._save_index(items)

    # -- 目录 --------------------------------------------------------------
    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def _meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def _read_meta(self, session_id: str) -> SessionMeta:
        import json

        path = self._meta_path(session_id)
        if not path.exists():
            raise KeyError(f"session_not_found: {session_id}")
        return SessionMeta.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _write_meta(self, meta: SessionMeta) -> None:
        atomic_write_json(self._meta_path(meta.id), meta.model_dump(mode="json"))

    # -- 事件 --------------------------------------------------------------
    def _next_seq(self, session_id: str) -> int:
        events = self.sink.read_events(session_id)
        return events[-1]["seq"] + 1 if events else 0

    def append(self, session_id: str, src: str, type_: str, payload: dict) -> int:
        seq = self._next_seq(session_id)
        self.sink.append_event(session_id, seq, src, type_, payload)
        return seq

    def append_event(self, session_id: str, event) -> None:
        mapped = wire_to_line(event)
        if mapped is None:
            return
        src, type_, payload = mapped
        self.append(session_id, src, type_, payload)

    def replay(self, session_id: str) -> list[dict]:
        return self.sink.read_events(session_id)

    def turn_count(self, session_id: str) -> int:
        return sum(1 for e in self.replay(session_id) if e.get("type") == "msg.user")

    # -- 生命周期 ----------------------------------------------------------
    def create(self, title: str | None, persona_id: str | None) -> SessionMeta:
        session_id = new_id(SESS)
        now = _utcnow()
        meta = SessionMeta(
            id=session_id,
            title=(title or "新对话"),
            persona_name=None,
            created_at=now,
            updated_at=now,
            state="active",
            main_model=None,
        )
        (self.session_dir(session_id) / "artifacts").mkdir(parents=True, exist_ok=True)
        self._write_meta(meta)
        self.append(session_id, "agent", "session.start", {"title": meta.title, "persona_id": persona_id})
        self._upsert_index(meta)
        return meta

    def get_meta(self, session_id: str) -> SessionMeta:
        return self._read_meta(session_id)

    def resume(self, session_id: str) -> SessionSnapshot:
        meta = self._read_meta(session_id)
        return SessionSnapshot(meta=meta, events=self.replay(session_id))

    def list(self, include_archived: bool = False) -> list[SessionMeta]:
        items = [SessionMeta.model_validate(i) for i in self._load_index()]
        if not include_archived:
            items = [m for m in items if m.state == "active"]
        items.sort(key=lambda m: m.updated_at, reverse=True)
        return items

    def _touch(self, meta: SessionMeta) -> None:
        meta.updated_at = _utcnow()
        self._write_meta(meta)
        self._upsert_index(meta)

    def touch(self, session_id: str) -> SessionMeta:
        meta = self._read_meta(session_id)
        self._touch(meta)
        return meta

    def rename(self, session_id: str, title: str) -> SessionMeta:
        meta = self._read_meta(session_id)
        meta.title = title
        self._touch(meta)
        self.append(session_id, "agent", "meta.update", {"title": title})
        return meta

    def set_model(self, session_id: str, model_id: str | None, slot: str = "main") -> SessionMeta:
        meta = self._read_meta(session_id)
        from_ = meta.main_model
        self.append(session_id, "user", "model.switch", {"slot": slot, "from": from_, "to": model_id})
        meta.main_model = model_id
        self._touch(meta)
        return meta

    def archive(self, session_id: str) -> SessionMeta:
        meta = self._read_meta(session_id)
        meta.state = "archived"
        self._touch(meta)
        return meta

    def unarchive(self, session_id: str) -> SessionMeta:
        meta = self._read_meta(session_id)
        meta.state = "active"
        self._touch(meta)
        return meta

    def delete(self, session_id: str) -> None:
        items = [i for i in self._load_index() if i.get("id") != session_id]
        self._save_index(items)
        self.sink.append_audit("session_delete", session_id=session_id)

    def end(self, session_id: str, reason: str) -> None:
        """收尾一个会话：落 `session.end` 并把事件流 fsync 到磁盘（docs 03 §10）。

        调用点：应用退出（`CoreController.shutdown`）。
        """
        self.append(session_id, "agent", "session.end", {"reason": reason})
        self.sink.fsync(session_id)
