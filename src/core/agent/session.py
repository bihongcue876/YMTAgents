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

from shared.envelope import BranchInfo, SessionMeta, SessionParams
from shared.ids import SESS, new_id
from shared.schema import BranchRecord, SessionGraph, SessionSummary

from core.bus.sink import EventSink
from core.store.atomic import atomic_write_json, atomic_write_text

#: 每会话最多分支数（rev31；用户裁决：最多 fork 五次，保障存储空间）。
MAX_BRANCHES = 5


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
        return (
            "agent",
            "msg.assistant.delta",
            {
                "content": d["content"],
                "turn_seq": d["turn_seq"],
                "reasoning": d.get("reasoning", False),
            },
        )
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
                "reasoning": d.get("reasoning", ""),
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
    def get_meta(self, session_id: str) -> SessionMeta:
        """读取会话 meta（rev23：controller 需要 persona_id / persona_name 解析）。"""

    @abstractmethod
    def set_persona(self, session_id: str, persona_id: str | None) -> SessionMeta:
        """会话级角色切换（spec rev23）：写 meta.persona_id 并落 `persona.switch` 事件。"""

    @abstractmethod
    def update(
        self,
        session_id: str,
        *,
        title: str,
        note: str,
        max_context: int | None,
        params: SessionParams,
        summary_threshold: int | None = None,
    ) -> SessionMeta:
        """整态更新会话可编辑字段（rev24）：标题/作用/上下文上限/模型参数。"""

    @abstractmethod
    def read_summary(self, session_id: str) -> SessionSummary | None:
        """读取摘要**状态**（rev26）；无摘要返回 None（正文另见 `read_summary_text`）。"""

    @abstractmethod
    def read_summary_text(self, session_id: str) -> str:
        """读取摘要正文 `summary.md`；不存在返回空串。"""

    @abstractmethod
    def write_summary(
        self,
        session_id: str,
        *,
        covered_seq: int,
        model: str | None,
        tokens_est: int,
        text: str,
    ) -> SessionSummary:
        """写入/更新摘要（rev26）：正文 + 状态，revision 自增。"""

    @abstractmethod
    def list_branches(self, session_id: str) -> SessionGraph:
        """读取分支树（rev31）；老会话惰性合成 br0。"""

    @abstractmethod
    def branch_info(self, session_id: str) -> list[BranchInfo]:
        """分支概览（rev31）：各分支活动前缀内轮数与末事件 seq，供右栏展示。"""

    @abstractmethod
    def create_branch(self, session_id: str, from_seq: int) -> SessionGraph:
        """从活动分支的 `from_seq`（含）处新开分支并设为活动（rev31）。

        超上限抛 `ValueError`；`from_seq` 不在活动前缀抛 `KeyError`。
        """

    @abstractmethod
    def revert_to(self, session_id: str, to_seq: int) -> SessionGraph:
        """把活动分支游标退到 `to_seq` 所在轮之前（rev31）；尾部 seq 保留。"""

    @abstractmethod
    def switch_branch(self, session_id: str, branch_id: str) -> SessionGraph:
        """切换活动分支（rev31）；未知分支抛 `KeyError`。"""

    @abstractmethod
    def data_bytes(self, session_id: str) -> int:
        """会话目录占用字节数（rev24 详情面板）。"""

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

    # -- 分支树（rev31）：graph.json 只记 seq 引用，事件本体永远在 events.jsonl --------
    def _graph_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "graph.json"

    def _load_graph(self, session_id: str) -> SessionGraph:
        import json

        path = self._graph_path(session_id)
        if path.exists():
            try:
                graph = SessionGraph.model_validate(json.loads(path.read_text(encoding="utf-8")))
                if graph.branches:
                    return graph
            except (OSError, ValueError):
                pass
        # 老会话（或无图）惰性合成 br0：引用既有全部事件，游标在末尾。
        seqs = [int(e["seq"]) for e in self.sink.read_events(session_id)]
        graph = SessionGraph(
            active="br0",
            branches=[
                BranchRecord(id="br0", parent=None, fork_seq=-1, created_at=_utcnow(),
                             events=seqs, cursor=len(seqs))
            ],
        )
        self._save_graph(session_id, graph)
        return graph

    def _save_graph(self, session_id: str, graph: SessionGraph) -> None:
        atomic_write_json(self._graph_path(session_id), graph.model_dump(mode="json"))

    def _active_branch(self, graph: SessionGraph) -> BranchRecord:
        for branch in graph.branches:
            if branch.id == graph.active:
                return branch
        return graph.branches[0]

    @staticmethod
    def _next_branch_id(graph: SessionGraph) -> str:
        numbers = [
            int(b.id[2:]) for b in graph.branches if b.id.startswith("br") and b.id[2:].isdigit()
        ]
        return f"br{(max(numbers) + 1) if numbers else 0}"

    # -- 事件 --------------------------------------------------------------
    def _next_seq(self, session_id: str) -> int:
        events = self.sink.read_events(session_id)
        return events[-1]["seq"] + 1 if events else 0

    def append(self, session_id: str, src: str, type_: str, payload: dict) -> int:
        seq = self._next_seq(session_id)
        self.sink.append_event(session_id, seq, src, type_, payload)
        graph = self._load_graph(session_id)
        branch = self._active_branch(graph)
        if branch.cursor < len(branch.events):
            # 回退后再发言：被回退的尾部不再属于本分支（事件本体仍在 events.jsonl）。
            branch.events = branch.events[: branch.cursor]
        branch.events.append(seq)
        branch.cursor = len(branch.events)
        self._save_graph(session_id, graph)
        return seq

    def append_event(self, session_id: str, event) -> None:
        mapped = wire_to_line(event)
        if mapped is None:
            return
        src, type_, payload = mapped
        self.append(session_id, src, type_, payload)

    def replay(self, session_id: str) -> list[dict]:
        """活动分支的转录（rev31）：按分支 `events[:cursor]` 过滤，天然保持 seq 升序。"""
        events = self.sink.read_events(session_id)
        graph = self._load_graph(session_id)
        branch = self._active_branch(graph)
        active = set(branch.events[: branch.cursor])
        # 必须按分支过滤：其它分支追加的事件也在同一份 events.jsonl 里（不可返回全量）。
        return [e for e in events if int(e.get("seq", -1)) in active]

    def turn_count(self, session_id: str) -> int:
        return sum(1 for e in self.replay(session_id) if e.get("type") == "msg.user")

    # -- 生命周期 ----------------------------------------------------------
    def create(self, title: str | None, persona_id: str | None) -> SessionMeta:
        session_id = new_id(SESS)
        now = _utcnow()
        meta = SessionMeta(
            id=session_id,
            title=(title or "新对话"),
            persona_id=persona_id,  # rev23：会话记住自己的角色（None = YMT 兜底）
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

    def set_persona(self, session_id: str, persona_id: str | None) -> SessionMeta:
        """会话级角色切换（spec rev23）：不同会话可各用各的角色。"""
        meta = self._read_meta(session_id)
        self.append(
            session_id, "user", "persona.switch", {"from": meta.persona_id, "to": persona_id}
        )
        meta.persona_id = persona_id
        self._touch(meta)
        return meta

    def get_meta(self, session_id: str) -> SessionMeta:
        return self._read_meta(session_id)

    def update(
        self,
        session_id: str,
        *,
        title: str,
        note: str,
        max_context: int | None,
        params: SessionParams,
        summary_threshold: int | None = None,
    ) -> SessionMeta:
        """整态更新（rev24）：面板提交完整期望状态，未变的字段原样回写。"""
        meta = self._read_meta(session_id)
        meta.title = title or meta.title
        meta.note = note or None
        meta.max_context = max_context
        meta.params = params
        meta.summary_threshold = summary_threshold
        self._touch(meta)
        self.append(
            session_id,
            "user",
            "meta.update",
            {
                "title": meta.title,
                "note": meta.note,
                "max_context": meta.max_context,
                "params": params.model_dump(mode="json"),
                "summary_threshold": meta.summary_threshold,
            },
        )
        return meta

    # -- 摘要（rev26 / rev31）：正文与状态分文件，按**分支**隔离；events.jsonl 只增不改 --
    def _branch_dir(self, session_id: str, branch_id: str) -> Path:
        return self.session_dir(session_id) / "branches" / branch_id

    def _summary_md_path(self, session_id: str, branch_id: str | None = None) -> Path:
        bid = branch_id or self._active_branch(self._load_graph(session_id)).id
        return self._branch_dir(session_id, bid) / "summary.md"

    def _summary_json_path(self, session_id: str, branch_id: str | None = None) -> Path:
        bid = branch_id or self._active_branch(self._load_graph(session_id)).id
        return self._branch_dir(session_id, bid) / "summary.json"

    def _read_summary_at(self, session_id: str, branch_id: str) -> SessionSummary | None:
        import json

        path = self._summary_json_path(session_id, branch_id)
        if not path.exists() and branch_id == "br0":
            legacy = self.session_dir(session_id) / "summary.json"  # rev26 旧布局
            if legacy.exists():
                path = legacy
        if not path.exists():
            return None
        try:
            return SessionSummary.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def read_summary(self, session_id: str) -> SessionSummary | None:
        bid = self._active_branch(self._load_graph(session_id)).id
        return self._read_summary_at(session_id, bid)

    def read_summary_text(self, session_id: str) -> str:
        bid = self._active_branch(self._load_graph(session_id)).id
        path = self._summary_md_path(session_id, bid)
        if not path.exists() and bid == "br0":
            legacy = self.session_dir(session_id) / "summary.md"  # rev26 旧布局
            if legacy.exists():
                path = legacy
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_summary(
        self,
        session_id: str,
        *,
        covered_seq: int,
        model: str | None,
        tokens_est: int,
        text: str,
    ) -> SessionSummary:
        now = _utcnow()
        bid = self._active_branch(self._load_graph(session_id)).id
        previous = self._read_summary_at(session_id, bid)
        summary = SessionSummary(
            revision=(previous.revision + 1) if previous else 1,
            covered_seq=covered_seq,
            model=model,
            tokens_est=tokens_est,
            created_at=(previous.created_at if previous else now),
            updated_at=now,
        )
        atomic_write_text(self._summary_md_path(session_id, bid), text)
        atomic_write_json(self._summary_json_path(session_id, bid), summary.model_dump(mode="json"))
        return summary

    def _copy_summary(self, session_id: str, parent_id: str, child_id: str, fork_seq: int) -> None:
        """分叉时把父分支摘要快照给子分支（rev31）；父摘要若已覆盖分叉点之后的内容则不继承。"""
        parent = self._read_summary_at(session_id, parent_id)
        if parent is None or parent.covered_seq > fork_seq:
            return
        text = ""
        src_md = self._summary_md_path(session_id, parent_id)
        if src_md.exists():
            try:
                text = src_md.read_text(encoding="utf-8")
            except OSError:
                return
        if text:
            atomic_write_text(self._summary_md_path(session_id, child_id), text)
        atomic_write_json(
            self._summary_json_path(session_id, child_id), parent.model_dump(mode="json")
        )

    # -- 分支操作（rev31） -----------------------------------------------------
    def list_branches(self, session_id: str) -> SessionGraph:
        return self._load_graph(session_id)

    def branch_info(self, session_id: str) -> list[BranchInfo]:
        graph = self._load_graph(session_id)
        types = {int(e["seq"]): e.get("type") for e in self.sink.read_events(session_id)}
        infos: list[BranchInfo] = []
        for branch in graph.branches:
            prefix = branch.events[: branch.cursor]
            turns = sum(1 for s in prefix if types.get(s) == "msg.user")
            infos.append(
                BranchInfo(
                    id=branch.id,
                    parent=branch.parent,
                    fork_seq=branch.fork_seq,
                    created_at=branch.created_at,
                    turns=turns,
                    head_seq=(prefix[-1] if prefix else -1),
                    active=(branch.id == graph.active),
                )
            )
        return infos

    def create_branch(self, session_id: str, from_seq: int) -> SessionGraph:
        graph = self._load_graph(session_id)
        if len(graph.branches) >= MAX_BRANCHES:
            raise ValueError("branch_limit")
        active = self._active_branch(graph)
        prefix = active.events[: active.cursor]
        if from_seq not in prefix:
            raise KeyError("fork_seq_not_found")
        index = prefix.index(from_seq)
        events = prefix[: index + 1]
        new_branch = BranchRecord(
            id=self._next_branch_id(graph),
            parent=active.id,
            fork_seq=from_seq,
            created_at=_utcnow(),
            events=events,
            cursor=len(events),
        )
        graph.branches.append(new_branch)
        graph.active = new_branch.id
        self._save_graph(session_id, graph)
        self._copy_summary(session_id, active.id, new_branch.id, from_seq)
        return graph

    def revert_to(self, session_id: str, to_seq: int) -> SessionGraph:
        graph = self._load_graph(session_id)
        active = self._active_branch(graph)
        prefix = active.events[: active.cursor]
        if to_seq not in prefix:
            raise KeyError("revert_seq_not_found")
        active.cursor = prefix.index(to_seq)  # 该轮起点之前；尾部 seq 保留
        self._save_graph(session_id, graph)
        return graph

    def switch_branch(self, session_id: str, branch_id: str) -> SessionGraph:
        graph = self._load_graph(session_id)
        if branch_id not in {b.id for b in graph.branches}:
            raise KeyError("branch_not_found")
        graph.active = branch_id
        self._save_graph(session_id, graph)
        return graph

    def data_bytes(self, session_id: str) -> int:
        directory = self.session_dir(session_id)
        if not directory.exists():
            return 0
        total = 0
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:  # 并发删除/权限等：跳过该文件，不影响统计
                continue
        return total

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
