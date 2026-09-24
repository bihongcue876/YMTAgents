"""单库/跨库混合检索：关键词召回 + 图命中，RRF 排序与可解释 trace。"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Literal

from core.library.graph import GraphStore
from core.library.line import LineStore
from core.library.models import SearchHit

SearchMode = Literal["hybrid", "events", "nodes"]
RRF_K = 60


def _age_factor(timestamp: str) -> float:
    try:
        text = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        created = datetime.fromisoformat(text)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
        return 0.5 + 0.5 * math.exp(-age_days / 365.0)
    except (TypeError, ValueError, OverflowError):
        return 0.5


def search_one(
    line: LineStore,
    graph: GraphStore,
    query: str,
    mode: SearchMode = "hybrid",
    top_k: int = 8,
) -> tuple[list[SearchHit], dict]:
    """在一个库中融合事件与节点召回；所有操作只读。"""
    top_k = max(1, min(int(top_k), 50))
    event_hits = [] if mode == "nodes" else line.search(query, max(top_k * 4, 20))
    node_hits = [] if mode == "events" else graph.search_nodes(query, max(top_k * 4, 20))
    ranked: dict[tuple[str, str], SearchHit] = {}
    traces: list[dict] = []

    for rank, event in enumerate(event_hits, start=1):
        score = _age_factor(event.created_at) / (RRF_K + rank)
        hit = SearchHit(
            kind="event",
            id=event.event_id,
            title=event.event_type,
            content=event.raw_content[:4000],
            content_hash=event.content_hash,
            score=score,
            source_refs=[event.event_id],
            trace=[{"channel": "line", "rank": rank, "rrf": score}],
        )
        ranked[(hit.kind, hit.id)] = hit
        traces.append({"kind": hit.kind, "id": hit.id, "rank": rank, "score": score})

    for rank, node in enumerate(node_hits, start=1):
        score = 1.0 / (RRF_K + rank)
        hit = SearchHit(
            kind="node",
            id=node.node_id,
            title=node.title,
            content=node.content[:4000],
            content_hash=hashlib.blake2s(
                f"{node.title.casefold()}\n{node.content.casefold()}".encode("utf-8"), digest_size=16
            ).hexdigest(),
            score=score,
            source_refs=list(node.source_refs),
            trace=[{"channel": "graph", "rank": rank, "rrf": score}],
        )
        ranked[(hit.kind, hit.id)] = hit
        traces.append({"kind": hit.kind, "id": hit.id, "rank": rank, "score": score})

    results = sorted(ranked.values(), key=lambda hit: (-hit.score, hit.kind, hit.id))[:top_k]
    return results, {
        "line_hits": len(event_hits),
        "graph_hits": len(node_hits),
        "fts5": line.fts_available,
        "channels": traces,
    }


def fuse_across_libraries(
    per_library: list[tuple[str, str, list[SearchHit], dict]], top_k: int = 8
) -> tuple[list[dict], list[dict]]:
    """等权库间 RRF；按内容哈希去重，合并且保留来源锚点。"""
    merged: dict[str, dict] = {}
    debug: list[dict] = []
    for library_id, library_name, hits, trace in per_library:
        debug.append({"library_id": library_id, "library_name": library_name, **trace})
        for rank, hit in enumerate(hits, start=1):
            key = hit.content_hash or hit.id
            score = 1.0 / (RRF_K + rank)
            item = merged.get(key)
            source = {
                "library_id": library_id,
                "library_name": library_name,
                "kind": hit.kind,
                "id": hit.id,
                "event_refs": list(hit.source_refs),
            }
            if item is None:
                item = {
                    "kind": hit.kind,
                    "id": hit.id,
                    "title": hit.title,
                    "content": hit.content,
                    "score": 0.0,
                    "source_refs": [],
                    "event_refs": [],
                    "source_libraries": [],
                    "trace": [],
                }
                merged[key] = item
            item["score"] += score
            if source not in item["source_refs"]:
                item["source_refs"].append(source)
            for event_ref in hit.source_refs:
                if event_ref not in item["event_refs"]:
                    item["event_refs"].append(event_ref)
            if library_id not in {ref["library_id"] for ref in item["source_libraries"]}:
                item["source_libraries"].append({"library_id": library_id, "library_name": library_name})
            item["trace"].append(
                {"library_id": library_id, "rank": rank, "score": score, "channels": hit.trace}
            )

    results = sorted(merged.values(), key=lambda row: (-row["score"], row["title"].casefold(), row["id"]))
    return results[: max(1, min(int(top_k), 50))], debug
