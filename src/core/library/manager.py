"""多库登记、双区存储访问与跨库统调（只在 DPIM 启用后导入）。"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shared.ids import LIB, new_id
from shared.schema import LibraryIndex, LibraryRecord

from core.library import paths
from core.library.graph import GraphStore, GraphStoreError
from core.library.line import LineStore
from core.library.models import EventType, GraphNode
from core.library.search import SearchMode, fuse_across_libraries, search_one
from core.store.atomic import backup_and_write_json

log = logging.getLogger(__name__)

MAX_LIBRARIES = 100
MAX_INDEX_BYTES = 4 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LibraryServiceError(RuntimeError):
    """登记或库数据不可用。"""


class ILibraryService(ABC):
    """CoreController 调用的 DPIM 宿主协作接口。"""

    @abstractmethod
    def list_libraries(self) -> list[dict]: ...

    @abstractmethod
    def current_library(self) -> str | None: ...

    @abstractmethod
    def create_library(self, name: str, root_kind: str = "managed", root: str | None = None,
                       group: str | None = None, model_ref: str = "main", note: str = "") -> str: ...

    @abstractmethod
    def update_library(self, library_id: str, **changes: Any) -> None: ...

    @abstractmethod
    def delete_library(self, library_id: str) -> None: ...

    @abstractmethod
    def switch_library(self, library_id: str) -> str | None: ...

    @abstractmethod
    def refresh_libraries(self, library_id: str | None = None) -> dict: ...

    @abstractmethod
    def library_detail(self, library_id: str, event_offset: int = 0, event_limit: int = 50,
                       graph_limit: int = 300, focus_event_id: str | None = None) -> dict: ...

    @abstractmethod
    def library_graph(self, library_ids: list[str] | None = None, limit: int = 1000) -> dict: ...

    @abstractmethod
    def ingest_event(self, library_id: str, text: str, event_type: EventType = "interaction",
                     *, event_id: str | None = None, index: bool = True) -> dict: ...

    @abstractmethod
    def query_libraries(self, query: str, library_ids: list[str] | None = None,
                        mode: SearchMode = "hybrid", top_k: int = 8) -> dict: ...


class LibraryManager:
    """本地多库协调器。

    只管理登记和存储，不直接依赖模型网关、GUI 或 Agent；DPIM 宿主负责 ingestion。
    文件句柄不跨调用保存，关闭宿主即可释放全部存储引用。
    """

    def __init__(self, data_root: Path, audit=None) -> None:
        self.data_root = Path(data_root)
        self.base = self.data_root / "libraries"
        self.index_path = self.base / "index.json"
        self._audit = audit or (lambda *_args, **_kwargs: None)
        self._index = LibraryIndex()
        self._current: str | None = None
        self._error = ""
        self._index_writable = True
        self._prepared: set[str] = set()

    @property
    def error(self) -> str:
        return self._error

    @property
    def current(self) -> str | None:
        return self._current

    def load(self) -> None:
        self._error = ""
        self._index_writable = True
        if self.base.is_symlink() or self.index_path.is_symlink():
            self._index = LibraryIndex()
            self._index_writable = False
            self._error = "书库登记目录或文件是符号链接，已拒绝读取。"
            return
        self.base.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._index = LibraryIndex()
            self._write_index()
            return
        try:
            if self.index_path.stat().st_size > MAX_INDEX_BYTES:
                raise LibraryServiceError("书库登记表超过 4 MiB 上限。")
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            index = LibraryIndex.model_validate(payload)
            self._validate_index(index)
        except (OSError, ValueError, ValidationError, LibraryServiceError) as exc:
            self._index = LibraryIndex()
            self._index_writable = False
            self._error = "小图书馆登记表不可读，已保留原文件；请修复 index.json 后刷新。"
            log.warning("DPIM 索引载入失败：%s", type(exc).__name__)
            return
        self._index = index
        if self._current not in {record.id for record in index.libraries}:
            self._current = None
        self._prepared.clear()
        # 迁移旧记录：分组目录键只生成一次并写入索引，不由显示名称构造。
        changed = False
        key_by_group = {
            record.group.casefold(): record.group_key
            for record in index.libraries
            if record.group and record.group_key
        }
        for record in index.libraries:
            if record.group and not record.group_key:
                record.group_key = key_by_group.setdefault(record.group.casefold(), new_id("grp"))
                changed = True
        if changed:
            self._write_index()

    def shutdown(self) -> None:
        self._prepared.clear()
        self._current = None
        self._index = LibraryIndex()

    def _validate_index(self, index: LibraryIndex) -> None:
        if len(index.libraries) > MAX_LIBRARIES:
            raise LibraryServiceError("书库数量超过上限。")
        ids: set[str] = set()
        roots: set[str] = set()
        groups: dict[str, str] = {}
        group_keys: dict[str, str] = {}
        for record in index.libraries:
            if record.id in ids:
                raise LibraryServiceError("书库登记表包含重复 id。")
            ids.add(record.id)
            if record.group:
                group_label = record.group.casefold()
                if record.group_key:
                    prior_key = groups.setdefault(group_label, record.group_key)
                    prior_group = group_keys.setdefault(record.group_key, group_label)
                    if prior_key != record.group_key or prior_group != group_label:
                        raise LibraryServiceError("分组显示名与生成目录键映射不唯一。")
            if record.root_kind == "external" and record.root:
                lexical = paths.abs_path(record.root)
                if not Path(record.root).is_absolute():
                    raise LibraryServiceError("外部书库 root 必须为绝对路径。")
                if lexical.is_symlink():
                    raise paths.LibraryDenied("外部书库 root 不得为符号链接。")
                if not lexical.exists():
                    key = paths.path_key(lexical)
                    if key in roots:
                        raise LibraryServiceError("书库登记表包含重复外部目录。")
                    roots.add(key)
                    continue
                try:
                    root = paths.validate_external_root(record.root, self.data_root)
                except paths.LibraryDenied:
                    raise
                except paths.LibraryPathError:
                    if not lexical.exists():
                        key = paths.path_key(lexical)
                        if key in roots:
                            raise LibraryServiceError("书库登记表包含重复外部目录。")
                        roots.add(key)
                        continue
                    raise
                key = paths.path_key(root)
                if key in roots:
                    raise LibraryServiceError("书库登记表包含重复外部目录。")
                roots.add(key)

    def _write_index(self) -> None:
        if not self._index_writable:
            raise LibraryServiceError(self._error or "登记表不可写；原文件已保留。")
        if self.index_path.is_symlink():
            raise paths.LibraryDenied("书库登记表不能是符号链接。")
        self.base.mkdir(parents=True, exist_ok=True)
        backup_and_write_json(self.index_path, self._index.model_dump(mode="json"))

    def _record(self, library_id: str) -> LibraryRecord:
        for record in self._index.libraries:
            if record.id == library_id:
                return record
        raise KeyError(library_id)

    def _record_root(self, record: LibraryRecord) -> Path:
        return paths.library_root(record, self.data_root)

    def _stores(self, record: LibraryRecord) -> tuple[LineStore, GraphStore]:
        root = self._record_root(record)
        return LineStore(root), GraphStore(root)

    def list_libraries(self) -> list[dict]:
        rows: list[dict] = []
        for record in self._index.libraries:
            row = record.model_dump(mode="json")
            try:
                row["resolved_root"] = str(self._record_root(record))
                row["missing"] = record.root_kind == "external" and not Path(record.root or "").is_dir()
            except paths.LibraryPathError as exc:
                row["resolved_root"] = record.root or ""
                row["missing"] = True
                row["root_error"] = str(exc)
            row["current"] = record.id == self._current
            rows.append(row)
        return rows

    def current_library(self) -> str | None:
        return self._current

    def _group_key(self, group: str | None, *, exclude_id: str | None = None) -> str | None:
        label = str(group or "").strip()
        if not label:
            return None
        for record in self._index.libraries:
            if (record.id != exclude_id and record.group
                    and record.group.casefold() == label.casefold() and record.group_key):
                return record.group_key
        return new_id("grp")

    def create_library(
        self,
        name: str,
        root_kind: str = "managed",
        root: str | None = None,
        group: str | None = None,
        model_ref: str = "main",
        note: str = "",
    ) -> str:
        if not self._index_writable:
            raise LibraryServiceError(self._error)
        if len(self._index.libraries) >= MAX_LIBRARIES:
            raise LibraryServiceError("书库数量已达上限。")
        label = str(name or "").strip()
        if not 1 <= len(label) <= 80:
            raise ValueError("书库名称必须为 1–80 个字符。")
        group_label = str(group or "").strip() or None
        if group_label and len(group_label) > 60:
            raise ValueError("分组名称不能超过 60 个字符。")
        ref = str(model_ref or "main").strip()
        if len(ref) > 200:
            raise ValueError("模型引用不能超过 200 个字符。")
        now = _utcnow()
        library_id = new_id(LIB)
        if root_kind == "external":
            external = paths.validate_external_root(str(root or ""), self.data_root)
            if any(
                record.root_kind == "external" and record.root and paths.path_key(record.root) == paths.path_key(external)
                for record in self._index.libraries
            ):
                raise paths.LibraryDenied("该目录已登记为另一个书库。")
            stored_root = str(external)
        elif root_kind == "managed":
            if root:
                raise ValueError("托管书库不接受自定义 root。")
            stored_root = None
        else:
            raise ValueError("root_kind 必须为 managed 或 external。")
        record = LibraryRecord(
            id=library_id,
            name=label,
            root_kind=root_kind,
            root=stored_root,
            group=group_label,
            group_key=self._group_key(group_label),
            model_ref=ref or "main",
            note=str(note or "").strip()[:1000],
            created_at=now,
            updated_at=now,
        )
        self._record_root(record)  # path containment / generated components check before persistence.
        self._index.libraries.append(record)
        try:
            self._write_index()
        except Exception:
            self._index.libraries.remove(record)
            raise
        self._audit("library.create", root_kind=root_kind, grouped=bool(group_label))
        return library_id

    def update_library(self, library_id: str, **changes: Any) -> None:
        record = self._record(library_id)
        old = record.model_copy(deep=True)
        try:
            old_root = self._record_root(record)
        except paths.LibraryPathError:
            old_root = Path(record.root or "")
        name = changes.get("name")
        if name is not None:
            label = str(name).strip()
            if not 1 <= len(label) <= 80:
                raise ValueError("书库名称必须为 1–80 个字符。")
            record.name = label
        group_changed = changes.get("group") is not None
        if group_changed:
            group_label = str(changes.get("group") or "").strip() or None
            if group_label and len(group_label) > 60:
                raise ValueError("分组名称不能超过 60 个字符。")
            record.group = group_label
            record.group_key = self._group_key(group_label, exclude_id=library_id)
        if "root" in changes and changes.get("root") is not None:
            if record.root_kind != "external":
                raise ValueError("只有外部书库可以修改 root。")
            new_root = paths.validate_external_root(str(changes["root"]), self.data_root)
            if any(
                other.id != library_id and other.root_kind == "external" and other.root
                and paths.path_key(other.root) == paths.path_key(new_root)
                for other in self._index.libraries
            ):
                raise paths.LibraryDenied("该目录已登记为另一个书库。")
            record.root = str(new_root)
        if changes.get("model_ref") is not None:
            ref = str(changes["model_ref"]).strip()
            if not ref or len(ref) > 200:
                raise ValueError("模型引用必须为 1–200 个字符。")
            record.model_ref = ref
        if changes.get("note") is not None:
            record.note = str(changes["note"]).strip()[:1000]
        record.updated_at = _utcnow()

        new_root = self._record_root(record)
        moved = False
        if group_changed and record.root_kind == "managed" and old_root.exists() and new_root != old_root:
            if new_root.exists():
                self._restore_record(library_id, old)
                raise paths.LibraryDenied("目标分组目录中已存在数据，拒绝覆盖。")
            new_root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_root, new_root)
            moved = True
        try:
            self._write_index()
        except Exception:
            self._restore_record(library_id, old)
            if moved and new_root.exists():
                old_root.parent.mkdir(parents=True, exist_ok=True)
                os.replace(new_root, old_root)
            raise
        self._prepared.discard(library_id)
        self._audit("library.update", fields=sorted(k for k, value in changes.items() if value is not None))

    def _restore_record(self, library_id: str, old: LibraryRecord) -> None:
        self._index.libraries = [old if record.id == library_id else record for record in self._index.libraries]

    def delete_library(self, library_id: str) -> None:
        record = self._record(library_id)
        self._index.libraries = [item for item in self._index.libraries if item.id != library_id]
        try:
            self._write_index()
        except Exception:
            self._index.libraries.append(record)
            raise
        self._prepared.discard(library_id)
        if self._current == library_id:
            self._current = None
        self._audit("library.delete", root_kind=record.root_kind)

    def switch_library(self, library_id: str) -> str | None:
        record = self._record(library_id)
        self._current = library_id
        if library_id not in self._prepared:
            try:
                line, graph = self._stores(record)
                graph.reconcile(line)
                self._prepared.add(library_id)
                self._error = ""
            except (OSError, ValueError, GraphStoreError, paths.LibraryPathError) as exc:
                self._error = str(exc) or "书库检查失败。"
                log.warning("DPIM 打开书库失败：%s", type(exc).__name__)
        self._audit("library.switch", root_kind=record.root_kind)
        return self._error or None

    def refresh_libraries(self, library_id: str | None = None) -> dict:
        self.load()
        if library_id is None:
            return {"libraries": len(self._index.libraries), "reconciled": 0, "error": self._error}
        record = self._record(library_id)
        line, graph = self._stores(record)
        result = graph.reconcile(line)
        self._prepared.add(library_id)
        self._audit("library.refresh", changed=bool(result["changed"]))
        return result

    def library_detail(
        self, library_id: str, event_offset: int = 0, event_limit: int = 50, graph_limit: int = 300,
        focus_event_id: str | None = None,
    ) -> dict:
        record = self._record(library_id)
        line, graph = self._stores(record)
        if focus_event_id:
            rank = line.offset_of(focus_event_id)
            if rank is not None:
                event_offset = (rank // event_limit) * event_limit
        events = line.page(event_offset, event_limit)
        counts = line.counts()
        graph_error = ""
        try:
            nodes, edges, truncated = graph.page(graph_limit)
            graph_counts = {"nodes": len(graph.load().nodes), "edges": len(graph.load().edges)}
        except GraphStoreError as exc:
            nodes, edges, truncated = [], [], False
            graph_counts = {"nodes": 0, "edges": 0}
            graph_error = str(exc)
        return {
            "id": library_id,
            "root": str(self._record_root(record)),
            "counts": {**counts, **graph_counts},
            "events": [event.model_dump(mode="json") for event in events],
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "edges": [edge.model_dump(mode="json") for edge in edges],
            "event_offset": event_offset,
            "event_limit": event_limit,
            "truncated": truncated,
            "error": graph_error,
        }

    def library_graph(self, library_ids: list[str] | None = None, limit: int = 1000) -> dict:
        selected = [record.id for record in self._index.libraries] if library_ids is None else list(library_ids)
        if len(selected) > 100:
            raise ValueError("一次图谱视图最多选择 100 个书库。")
        selected = list(dict.fromkeys(selected))
        nodes: list[dict] = []
        edges: list[dict] = []
        truncated = False
        remaining = max(1, min(int(limit), 1000))
        for library_id in selected:
            record = self._record(library_id)
            _line, graph = self._stores(record)
            try:
                lib_nodes, lib_edges, lib_truncated = graph.page(remaining)
            except GraphStoreError:
                truncated = True
                continue
            node_rows = [
                {**node.model_dump(mode="json"), "library_id": record.id, "library_name": record.name}
                for node in lib_nodes
            ]
            ids = {row["node_id"] for row in node_rows}
            edge_rows = [
                {**edge.model_dump(mode="json"), "library_id": record.id}
                for edge in lib_edges if edge.source in ids and edge.target in ids
            ]
            nodes.extend(node_rows)
            edges.extend(edge_rows)
            remaining -= len(node_rows)
            truncated = truncated or lib_truncated or remaining <= 0
            if remaining <= 0:
                break
        return {"library_ids": selected, "nodes": nodes, "edges": edges, "truncated": truncated}

    def append_event(self, library_id: str, text: str, event_type: EventType = "interaction") -> dict:
        record = self._record(library_id)
        line, _graph = self._stores(record)
        event = line.append(new_id("evt"), text, event_type)
        self._audit("library.ingest", event_type=event_type, chars=len(text))
        return event.model_dump(mode="json")

    def load_event(self, library_id: str, event_id: str):
        record = self._record(library_id)
        line, _graph = self._stores(record)
        event = line.get(event_id)
        if event is None:
            raise KeyError(event_id)
        return event

    def transition_event(self, library_id: str, event_id: str, status: str,
                         graph_refs: list[str] | None = None, error: str = "") -> None:
        self.event_status(library_id, event_id, status, graph_refs, error)

    def soft_delete_event(self, library_id: str, event_id: str) -> dict[str, int]:
        record = self._record(library_id)
        line, graph = self._stores(record)
        if line.get(event_id) is None:
            raise KeyError(event_id)
        result = graph.detach_event(event_id)
        line.transition(event_id, "skipped", graph_refs=[], error="soft_deleted")
        self._audit("library.event.soft_delete", detached=result["nodes_detached"], removed=result["nodes_removed"])
        return result

    def add_manual_node(self, library_id: str, title: str, content: str) -> str:
        record = self._record(library_id)
        _line, graph = self._stores(record)
        node = GraphNode(node_id=new_id("node"), title=title, content=content, node_type="system")
        graph.upsert([node], [])
        self._audit("library.node.add", nodes=1)
        return node.node_id

    def delete_node(self, library_id: str, node_id: str) -> dict[str, int]:
        record = self._record(library_id)
        _line, graph = self._stores(record)
        result = graph.delete_node(node_id)
        self._audit("library.node.delete", nodes=result["nodes_removed"], edges=result["edges_removed"])
        return result

    def merge_nodes(self, library_id: str, source_id: str, target_id: str) -> dict[str, int]:
        record = self._record(library_id)
        _line, graph = self._stores(record)
        result = graph.merge_nodes(source_id, target_id)
        self._audit("library.node.merge", nodes=result["nodes_merged"], edges=result["edges_removed"])
        return result

    def compact_graph(self, library_id: str) -> dict[str, int]:
        record = self._record(library_id)
        _line, graph = self._stores(record)
        result = graph.compact()
        self._audit("library.graph.compact", nodes=result["nodes_removed"], edges=result["edges_removed"])
        return result

    def event_status(self, library_id: str, event_id: str, status: str,
                     graph_refs: list[str] | None = None, error: str = "") -> None:
        record = self._record(library_id)
        line, _graph = self._stores(record)
        line.transition(event_id, status, graph_refs=graph_refs, error=error)

    def query(
        self,
        query: str,
        library_ids: list[str] | None = None,
        mode: SearchMode = "hybrid",
        top_k: int = 8,
    ) -> dict:
        cleaned = str(query or "").strip()
        if not cleaned:
            raise ValueError("检索内容不能为空。")
        if len(cleaned) > 4000:
            raise ValueError("检索内容不能超过 4000 个字符。")
        selected = [record.id for record in self._index.libraries] if library_ids is None else list(library_ids)
        if len(selected) > 100:
            raise ValueError("一次联合查询最多覆盖 100 个书库。")
        selected = list(dict.fromkeys(selected))
        per_library: list[tuple[str, str, list, dict]] = []
        errors: list[dict] = []
        for library_id in selected:
            record = self._record(library_id)
            line, graph = self._stores(record)
            try:
                hits, debug = search_one(line, graph, cleaned, mode, max(top_k, 8))
            except GraphStoreError as exc:
                # 图层损坏不牺牲线层全文检索；错误类型只进入界面/debug，不含原始数据。
                if mode == "nodes":
                    hits = []
                    line_count = 0
                else:
                    event_hits = line.search(cleaned, max(top_k * 4, 20))
                    from core.library.models import SearchHit

                    hits = [
                        SearchHit(
                            kind="event", id=event.event_id, title=event.event_type,
                            content=event.raw_content[:4000], content_hash=event.content_hash,
                            score=1.0 / (60 + rank), source_refs=[event.event_id],
                            trace=[{"channel": "line", "rank": rank}],
                        )
                        for rank, event in enumerate(event_hits, start=1)
                    ]
                    line_count = len(event_hits)
                debug = {"line_hits": line_count, "graph_hits": 0, "graph_error": str(exc)}
            per_library.append((record.id, record.name, hits, debug))
        results, debug = fuse_across_libraries(per_library, top_k)
        return {"query": cleaned, "library_ids": selected, "results": results, "debug": debug, "errors": errors}
