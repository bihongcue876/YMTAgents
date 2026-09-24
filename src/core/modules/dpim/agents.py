"""DPIM 的 Cr → In → Gr → Meta 索引流水线。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.ids import new_id
from shared.redact import redact

from core.library.graph import GraphStore
from core.library.line import LineStore
from core.library.models import GraphEdge, GraphNode, LibraryEvent
from core.modules.dpim.parse import AgentOutputError, parse_object

PROMPTS = Path(__file__).resolve().parent / "prompts"
MAX_MODEL_INPUT = 18_000
MAX_NODES_PER_EVENT = 20
MAX_EDGES_PER_EVENT = 40


class IndexingError(RuntimeError):
    """一个或多个结构化索引阶段未能给出可用结果。"""


class DpimIndexer:
    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway
        self._turn_seq = 0

    def _call(self, role: str, model_id: str, library_id: str, payload: dict) -> dict:
        try:
            prompt = (PROMPTS / f"{role}.md").read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise IndexingError("索引提示词文件不可读。") from exc
        self._turn_seq += 1
        chunks: list[str] = []
        try:
            self._gateway.stream_chat(
                session_id=f"library_{library_id}",
                turn_seq=self._turn_seq,
                model_id=model_id,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                cancel_token=None,
                on_delta=chunks.append,
            )
        except Exception as exc:  # 网关细节不能进入库状态/界面
            raise IndexingError("索引模型调用失败。") from exc
        try:
            return parse_object("".join(chunks))
        except AgentOutputError as exc:
            raise IndexingError("索引模型返回了无效结构。") from exc

    def index(self, library_id: str, event: LibraryEvent, model_id: str,
              line: LineStore, graph: GraphStore) -> dict:
        source_text = (redact(event.raw_content) or "")[:MAX_MODEL_INPUT]
        cr = self._call("core", model_id, library_id, {"content": source_text})
        summary = str(cr.get("summary") or "").strip()[:2000]
        intent = str(cr.get("intent") or "").strip()[:500]
        keywords = cr.get("keywords")
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(word).strip()[:80] for word in keywords[:30] if str(word).strip()]

        extraction = self._call(
            "infomater", model_id, library_id,
            {"content": source_text, "summary": summary, "intent": intent, "keywords": keywords},
        )
        items = extraction.get("items")
        if not isinstance(items, list):
            raise IndexingError("Infomater 未返回条目列表。")
        candidates: list[GraphNode] = []
        for raw in items[:MAX_NODES_PER_EVENT]:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()[:60]
            content = str(raw.get("content") or "").strip()[:5000]
            node_type = str(raw.get("node_type") or "data")
            if not title or not content or node_type not in {"system", "interaction", "data"}:
                continue
            try:
                confidence = float(raw.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            confidence = max(0.0, min(1.0, confidence))
            candidates.append(
                GraphNode(
                    node_id=new_id("node"),
                    title=title,
                    content=content,
                    node_type=node_type,
                    source_refs=[event.event_id],
                    confidence=confidence,
                )
            )
        if not candidates:
            line.transition(event.event_id, "indexed", graph_refs=[])
            return {"status": "indexed", "nodes": 0, "edges": 0, "summary": summary}

        graphing = self._call(
            "grapher", model_id, library_id,
            {
                "summary": summary,
                "nodes": [{"title": node.title, "content": node.content} for node in candidates],
            },
        )
        proposed_edges = graphing.get("edges")
        if not isinstance(proposed_edges, list):
            proposed_edges = []
        nodes_by_title = {node.title: node for node in candidates}
        edges: list[GraphEdge] = []
        for raw in proposed_edges[:MAX_EDGES_PER_EVENT]:
            if not isinstance(raw, dict):
                continue
            source = str(raw.get("source_title") or "").strip()
            target = str(raw.get("target_title") or "").strip()
            relation = str(raw.get("relation") or "").strip()[:80]
            if source not in nodes_by_title or target not in nodes_by_title or not relation:
                continue
            edges.append(
                GraphEdge(
                    source=nodes_by_title[source].node_id,
                    target=nodes_by_title[target].node_id,
                    relation=relation,
                    evidence_event_id=event.event_id,
                    note=str(raw.get("note") or "").strip()[:1000],
                )
            )

        review = self._call(
            "metacognition", model_id, library_id,
            {
                "source": source_text,
                "nodes": [{"title": node.title, "content": node.content} for node in candidates],
                "edges": [
                    {
                        "source_title": next(n.title for n in candidates if n.node_id == edge.source),
                        "target_title": next(n.title for n in candidates if n.node_id == edge.target),
                        "relation": edge.relation,
                    }
                    for edge in edges
                ],
            },
        )
        accepted_titles = review.get("accepted_titles")
        accepted_edges = review.get("accepted_edges")
        if not isinstance(accepted_titles, list) or not isinstance(accepted_edges, list):
            raise IndexingError("Meta 未返回有效审核清单。")
        allowed_titles = {str(title).strip() for title in accepted_titles}
        accepted_nodes = [node for node in candidates if node.title in allowed_titles]
        accepted_node_ids = {node.node_id for node in accepted_nodes}
        allowed_edges = {
            (
                str(edge.get("source_title") or "").strip(),
                str(edge.get("target_title") or "").strip(),
                str(edge.get("relation") or "").strip().casefold(),
            )
            for edge in accepted_edges if isinstance(edge, dict)
        }
        accepted_edges_final = [
            edge for edge in edges
            if edge.source in accepted_node_ids and edge.target in accepted_node_ids
            and (
                next(node.title for node in candidates if node.node_id == edge.source),
                next(node.title for node in candidates if node.node_id == edge.target),
                edge.relation.casefold(),
            ) in allowed_edges
        ]

        document = graph.upsert(accepted_nodes, accepted_edges_final)
        event_refs = [
            node.node_id for node in document.nodes if event.event_id in node.source_refs
        ]
        line.transition(event.event_id, "linked" if event_refs else "indexed", graph_refs=event_refs)
        return {
            "status": "linked" if event_refs else "indexed",
            "nodes": len(event_refs),
            "edges": len(accepted_edges_final),
            "summary": summary,
        }
