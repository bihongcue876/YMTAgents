"""小图书馆双区数据模型（非 GUI、非配置文件）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


EventStatus = Literal["raw", "indexed", "linked", "failed", "skipped"]
EventType = Literal["interaction", "data", "source"]
NodeType = Literal["system", "interaction", "data"]


class LibraryEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    created_at: str = Field(max_length=64)
    raw_content: str = Field(max_length=100_000)
    content_hash: str = Field(max_length=64)
    event_type: EventType
    status: EventStatus = "raw"
    graph_refs: list[str] = Field(default_factory=list)
    error: str = Field(default="", max_length=500)


class GraphNode(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=60)
    content: str = Field(default="", max_length=20_000)
    node_type: NodeType = "data"
    source_refs: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=500
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class GraphEdge(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    relation: str = Field(min_length=1, max_length=80)
    evidence_event_id: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=2000)


class GraphDocument(BaseModel):
    schema_version: Literal[1] = 1
    nodes: list[GraphNode] = Field(default_factory=list, max_length=100_000)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=300_000)


class SearchHit(BaseModel):
    kind: Literal["event", "node"]
    id: str
    title: str = ""
    content: str
    content_hash: str
    score: float
    source_refs: list[str] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
