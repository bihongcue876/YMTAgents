"""信息图层：原子 JSON 派生读模型，线层事件是唯一来源锚点。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from shared.redact import redact

from core.library.line import LineStore
from core.library.models import GraphDocument, GraphEdge, GraphNode
from core.library.paths import ensure_regular_data_file
from core.store.atomic import atomic_write_text, backup_file

log = logging.getLogger(__name__)
MAX_GRAPH_BYTES = 64 * 1024 * 1024


class GraphStoreError(RuntimeError):
    """图层损坏且没有可读备份，必须显式重建，不能覆盖静默清空。"""


class GraphStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = ensure_regular_data_file(self.root, "graph.json")
        self.recovered_from_backup = False

    @property
    def backup_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".bak")

    def load(self) -> GraphDocument:
        self.path = ensure_regular_data_file(self.root, "graph.json")
        self.recovered_from_backup = False
        if self.backup_path.is_symlink():
            raise GraphStoreError("图层备份是符号链接，已拒绝读取。")
        if not self.path.exists():
            if self.backup_path.exists():
                try:
                    document = self._read(self.backup_path)
                    self.recovered_from_backup = True
                    return document
                except (OSError, ValueError, ValidationError) as exc:
                    raise GraphStoreError("graph.json 缺失且备份不可读；原备份已保留。") from exc
            return GraphDocument()
        try:
            return self._read(self.path)
        except (OSError, ValueError, ValidationError, GraphStoreError) as primary_error:
            if self.backup_path.exists():
                try:
                    document = self._read(self.backup_path)
                    self.recovered_from_backup = True
                    log.warning("graph.json 损坏，当前使用 .bak；待显式修复")
                    return document
                except (OSError, ValueError, ValidationError, GraphStoreError):
                    pass
            raise GraphStoreError("图层文件与备份均不可读；原文件已保留，需显式重建。") from primary_error

    @staticmethod
    def _read(path: Path) -> GraphDocument:
        if path.stat().st_size > MAX_GRAPH_BYTES:
            raise GraphStoreError("图层文件超过 64 MiB 读取上限。")
        return GraphDocument.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def save(self, document: GraphDocument) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = ensure_regular_data_file(self.root, "graph.json")
        payload = document.model_dump(mode="json")
        for node in payload["nodes"]:
            node["title"] = redact(node.get("title")) or ""
            node["content"] = redact(node.get("content")) or ""
        for edge in payload["edges"]:
            edge["relation"] = redact(edge.get("relation")) or ""
            edge["note"] = redact(edge.get("note")) or ""
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if len(encoded.encode("utf-8")) > MAX_GRAPH_BYTES:
            raise GraphStoreError("图层写入超过 64 MiB 上限。")
        if self.recovered_from_backup and self.backup_path.exists():
            # 不用损坏的 primary 覆盖刚读出的有效 .bak。
            atomic_write_text(self.path, encoded)
        else:
            # 先备份旧文件，再复用项目公共原子文本写入，避免重复 JSON 序列化。
            backup_file(self.path)
            atomic_write_text(self.path, encoded)
        self.recovered_from_backup = False

    def upsert(self, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]) -> GraphDocument:
        document = self.load()
        by_title = {(node.node_type, node.title.casefold()): node for node in document.nodes}
        remap: dict[str, str] = {}
        for incoming in nodes:
            key = (incoming.node_type, incoming.title.casefold())
            old = by_title.get(key)
            if old is None:
                document.nodes.append(incoming)
                by_title[key] = incoming
                remap[incoming.node_id] = incoming.node_id
                continue
            old.content = incoming.content or old.content
            old.confidence = max(old.confidence, incoming.confidence)
            old.source_refs = list(dict.fromkeys([*old.source_refs, *incoming.source_refs]))[:500]
            remap[incoming.node_id] = old.node_id

        known = {node.node_id for node in document.nodes}
        edge_keys = {(e.source, e.target, e.relation.casefold()) for e in document.edges}
        for edge in edges:
            source = remap.get(edge.source, edge.source)
            target = remap.get(edge.target, edge.target)
            if source not in known or target not in known:
                continue
            key = (source, target, edge.relation.casefold())
            if key in edge_keys:
                continue
            document.edges.append(
                GraphEdge(
                    source=source,
                    target=target,
                    relation=edge.relation,
                    evidence_event_id=edge.evidence_event_id,
                    note=edge.note,
                )
            )
            edge_keys.add(key)
        self.save(document)
        return document

    def reconcile(self, line: LineStore) -> dict[str, int | bool]:
        """清理失效反向引用并从图层重建事件反向索引；只由显式切换/刷新调用。"""
        document = self.load()
        valid_ids = line.all_ids()
        changed = False
        refs_by_event: dict[str, list[str]] = {event_id: [] for event_id in valid_ids}
        for node in document.nodes:
            refs = list(dict.fromkeys(ref for ref in node.source_refs if ref in valid_ids))
            if refs != node.source_refs:
                node.source_refs = refs
                changed = True
            for event_id in refs:
                refs_by_event.setdefault(event_id, []).append(node.node_id)
        if changed or self.recovered_from_backup:
            self.save(document)
        line.set_graph_refs(refs_by_event)
        return {
            "events": len(valid_ids),
            "nodes": len(document.nodes),
            "edges": len(document.edges),
            "changed": changed or self.recovered_from_backup,
        }

    def detach_event(self, event_id: str) -> dict[str, int]:
        """软删除来源事件的派生图引用，不修改线层原文。"""
        document = self.load()
        affected: set[str] = set()
        removed: set[str] = set()
        for node in document.nodes:
            if event_id not in node.source_refs:
                continue
            affected.add(node.node_id)
            node.source_refs = [ref for ref in node.source_refs if ref != event_id]
            if not node.source_refs:
                removed.add(node.node_id)
        before_edges = len(document.edges)
        if not affected and not any(edge.evidence_event_id == event_id for edge in document.edges):
            return {"nodes_detached": 0, "nodes_removed": 0, "edges_removed": 0}
        document.nodes = [node for node in document.nodes if node.node_id not in removed]
        document.edges = [
            edge for edge in document.edges
            if edge.source not in removed and edge.target not in removed and edge.evidence_event_id != event_id
        ]
        self.save(document)
        return {
            "nodes_detached": len(affected),
            "nodes_removed": len(removed),
            "edges_removed": before_edges - len(document.edges),
        }

    def delete_node(self, node_id: str) -> dict[str, int]:
        document = self.load()
        if not any(node.node_id == node_id for node in document.nodes):
            raise KeyError(node_id)
        before_edges = len(document.edges)
        document.nodes = [node for node in document.nodes if node.node_id != node_id]
        document.edges = [edge for edge in document.edges if edge.source != node_id and edge.target != node_id]
        self.save(document)
        return {"nodes_removed": 1, "edges_removed": before_edges - len(document.edges)}

    def merge_nodes(self, source_id: str, target_id: str) -> dict[str, int]:
        document = self.load()
        by_id = {node.node_id: node for node in document.nodes}
        if source_id not in by_id or target_id not in by_id:
            raise KeyError(source_id if source_id not in by_id else target_id)
        if source_id == target_id:
            raise ValueError("不能把节点合并到自身。")
        source, target = by_id[source_id], by_id[target_id]
        if target.content and target.content not in source.content:
            source.content = (source.content + "\n\n" + target.content).strip()[:20_000]
        source.source_refs = list(dict.fromkeys([*source.source_refs, *target.source_refs]))[:500]
        source.confidence = max(source.confidence, target.confidence)
        before_edges = len(document.edges)
        remapped: list[GraphEdge] = []
        keys: set[tuple[str, str, str]] = set()
        for edge in document.edges:
            left = source_id if edge.source == target_id else edge.source
            right = source_id if edge.target == target_id else edge.target
            if left == right:
                continue
            key = (left, right, edge.relation.casefold())
            if key in keys:
                continue
            keys.add(key)
            remapped.append(edge.model_copy(update={"source": left, "target": right}))
        document.nodes = [node for node in document.nodes if node.node_id != target_id]
        document.edges = remapped
        self.save(document)
        return {"nodes_merged": 1, "edges_removed": before_edges - len(remapped)}

    def compact(self) -> dict[str, int]:
        document = self.load()
        by_semantic: dict[tuple[str, str], GraphNode] = {}
        remap: dict[str, str] = {}
        for node in document.nodes:
            key = (node.node_type, node.title.casefold())
            primary = by_semantic.get(key)
            if primary is None:
                by_semantic[key] = node
                remap[node.node_id] = node.node_id
            else:
                if node.content and node.content not in primary.content:
                    primary.content = (primary.content + "\n\n" + node.content).strip()[:20_000]
                primary.source_refs = list(dict.fromkeys([*primary.source_refs, *node.source_refs]))[:500]
                primary.confidence = max(primary.confidence, node.confidence)
                remap[node.node_id] = primary.node_id
        before_nodes, before_edges = len(document.nodes), len(document.edges)
        document.nodes = list(by_semantic.values())
        edge_keys: set[tuple[str, str, str]] = set()
        edges: list[GraphEdge] = []
        for edge in document.edges:
            source = remap.get(edge.source, edge.source)
            target = remap.get(edge.target, edge.target)
            if source == target:
                continue
            key = (source, target, edge.relation.casefold())
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edges.append(edge.model_copy(update={"source": source, "target": target}))
        document.edges = edges
        self.save(document)
        return {"nodes_removed": before_nodes - len(document.nodes), "edges_removed": before_edges - len(edges)}

    def page(self, limit: int = 1000) -> tuple[list[GraphNode], list[GraphEdge], bool]:
        document = self.load()
        limit = max(1, min(int(limit), 1000))
        truncated = len(document.nodes) > limit
        nodes = document.nodes[:limit]
        node_ids = {node.node_id for node in nodes}
        edges = [edge for edge in document.edges if edge.source in node_ids and edge.target in node_ids]
        return nodes, edges, truncated or len(edges) < len(document.edges)

    def counts(self) -> dict[str, int]:
        document = self.load()
        return {"nodes": len(document.nodes), "edges": len(document.edges)}

    def search_nodes(self, query: str, limit: int = 50) -> list[GraphNode]:
        document = self.load()
        terms = [term.casefold() for term in query.split() if term]
        if not terms:
            terms = [query.casefold()] if query.strip() else []
        if not terms:
            return []
        scored: list[tuple[int, GraphNode]] = []
        for node in document.nodes:
            haystack = f"{node.title}\n{node.content}".casefold()
            score = sum(haystack.count(term) for term in terms)
            if score:
                scored.append((score, node))
        scored.sort(key=lambda item: (-item[0], item[1].title.casefold(), item[1].node_id))
        return [node for _score, node in scored[: max(1, min(int(limit), 200))]]
