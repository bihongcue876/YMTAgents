"""信封与共享类型（spec §1.1 / §1.2）。

- `Envelope` 为基类，提供 `v/seq/ts`。
- 请求信封（UI → core）与事件信封（core → UI）均继承 `Envelope`。
- `Request` / `Event` 为带 `type` 判别器的联合类型，供 `parse_request` / `parse_event` 校验。
- 本模块只依赖标准库与 pydantic，禁止 import 项目内其它包。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, field_serializer


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Envelope(BaseModel):
    """信封基类。ts 序列化为 ISO 8601 UTC 毫秒（docs 03 §1）。"""

    v: Literal[1] = 1
    seq: int = 0
    ts: datetime = Field(default_factory=_now)

    @field_serializer("ts")
    def _ser_ts(self, dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# 支撑类型（spec §1.2）
# ---------------------------------------------------------------------------
class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ModelSpec(BaseModel):
    id: str
    ctx_window: int = 0  # 0 = 未知
    tags: list[str] = Field(default_factory=list)


class ProviderSpec(BaseModel):
    """下发到 GUI 的供应商视图；永不携带密钥明文（只带 key_status）。"""

    id: str
    name: str
    base_url: str
    models: list[ModelSpec] = Field(default_factory=list)
    key_status: Literal["stored", "missing", "error"] = "missing"


class SessionMeta(BaseModel):
    id: str
    title: str
    persona_name: str | None = None  # rev2：首期恒 None
    created_at: datetime
    updated_at: datetime
    state: Literal["active", "archived"] = "active"
    main_model: str | None = None


# ---------------------------------------------------------------------------
# 请求信封（UI → core，spec §1.1）
# ---------------------------------------------------------------------------
class SendMessage(Envelope):
    type: Literal["msg.user"] = "msg.user"
    text: str
    attachments: list[str] = Field(default_factory=list)  # 相对 workspace/files 的路径


class CancelTurn(Envelope):
    type: Literal["turn.cancel"] = "turn.cancel"
    reason: Literal["user"] = "user"


class SwitchModel(Envelope):
    type: Literal["model.switch"] = "model.switch"
    slot: Literal["main", "thinking", "fast", "embedding"] = "main"
    model_id: str | None = None  # None = 回退到槽位默认


class SetSlot(Envelope):
    """全局槽位绑定（spec rev4 §1）。

    与 SwitchModel 的作用域区分（spec rev4 §2）：
    - SetSlot：全局配置层，写 models.json 的 slots，供模型配置页「槽位绑定区」使用；
    - SwitchModel：会话实例层，写会话 meta.main_model 并发 model.switch 事件。
    """

    type: Literal["slot.set"] = "slot.set"
    slot: Literal["main", "thinking", "fast", "embedding"] = "main"
    model_id: str | None = None  # None = 清空该槽位绑定


class NewSession(Envelope):
    type: Literal["session.new"] = "session.new"
    title: str | None = None
    persona_id: str | None = None  # rev2：首期恒 None


class ResumeSession(Envelope):
    type: Literal["session.resume"] = "session.resume"
    session_id: str


class ArchiveSession(Envelope):
    type: Literal["session.archive"] = "session.archive"
    session_id: str


class UnarchiveSession(Envelope):
    type: Literal["session.unarchive"] = "session.unarchive"
    session_id: str


class RenameSession(Envelope):
    type: Literal["session.rename"] = "session.rename"
    session_id: str
    title: str


class DeleteSession(Envelope):
    type: Literal["session.delete"] = "session.delete"
    session_id: str


class ProviderUpsert(Envelope):
    type: Literal["provider.upsert"] = "provider.upsert"
    provider: ProviderSpec
    api_key: str | None = None  # 仅本次提交携带；None = 保持不变（spec 补充）


class ProviderDelete(Envelope):
    type: Literal["provider.delete"] = "provider.delete"
    provider_id: str


class TestConnection(Envelope):
    type: Literal["provider.test"] = "provider.test"
    provider_id: str
    model_id: str

    __test__ = False  # 阻止 pytest 误收集


class SettingsUpdate(Envelope):
    type: Literal["settings.update"] = "settings.update"
    section: Literal["context", "network", "logging", "ui"] = "context"
    data: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 事件信封（core → UI，spec §1.1）
# ---------------------------------------------------------------------------
class AssistantDelta(Envelope):
    type: Literal["msg.assistant.delta"] = "msg.assistant.delta"
    content: str
    turn_seq: int = 0


class AssistantFinal(Envelope):
    type: Literal["msg.assistant.final"] = "msg.assistant.final"
    content: str
    turn_seq: int = 0
    usage: Usage = Field(default_factory=Usage)
    interrupted: bool = False
    truncated: bool = False


class TurnStatus(Envelope):
    type: Literal["turn.status"] = "turn.status"
    turn_seq: int = 0
    state: Literal["assembling", "calling", "done", "failed", "interrupted"] = "assembling"
    error: str | None = None


class ContextUsage(Envelope):
    type: Literal["ctx.usage"] = "ctx.usage"
    segments: dict[
        Literal["system", "env", "files", "retrieve", "history", "reserve"], int
    ] = Field(default_factory=dict)
    total: int = 0
    window: int = 0


class SessionCreated(Envelope):
    type: Literal["session.created"] = "session.created"
    session_id: str
    title: str
    persona_name: str | None = None
    created_at: datetime


class SessionIndex(Envelope):
    type: Literal["session.index"] = "session.index"
    sessions: list[SessionMeta] = Field(default_factory=list)


class SessionEvents(Envelope):
    type: Literal["session.events"] = "session.events"
    session_id: str
    events: list[dict] = Field(default_factory=list)


class ProviderList(Envelope):
    type: Literal["provider.list"] = "provider.list"
    providers: list[ProviderSpec] = Field(default_factory=list)
    slots: dict[Literal["main", "thinking", "fast", "embedding"], str | None] = Field(
        default_factory=dict
    )


class TestResult(Envelope):
    type: Literal["provider.test.result"] = "provider.test.result"
    provider_id: str
    model_id: str
    ok: bool
    latency_ms: int | None = None
    error: str | None = None


class HealthReport(Envelope):
    type: Literal["health.report"] = "health.report"
    modules: dict[str, str] = Field(default_factory=dict)
    main_model: str | None = None
    slot_ready: bool = False


class ErrorReport(Envelope):
    type: Literal["error"] = "error"
    scope: Literal["session", "config", "gateway", "system"] = "system"
    code: str = "internal"
    message: str = ""
    detail: str | None = None


class SettingsState(Envelope):
    type: Literal["settings.state"] = "settings.state"
    data: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 联合类型 + 校验器
# ---------------------------------------------------------------------------
Request = Annotated[
    Union[
        SendMessage,
        CancelTurn,
        SwitchModel,
        SetSlot,
        NewSession,
        ResumeSession,
        ArchiveSession,
        UnarchiveSession,
        RenameSession,
        DeleteSession,
        ProviderUpsert,
        ProviderDelete,
        TestConnection,
        SettingsUpdate,
    ],
    Field(discriminator="type"),
]

Event = Annotated[
    Union[
        AssistantDelta,
        AssistantFinal,
        TurnStatus,
        ContextUsage,
        SessionCreated,
        SessionIndex,
        SessionEvents,
        ProviderList,
        TestResult,
        HealthReport,
        ErrorReport,
        SettingsState,
    ],
    Field(discriminator="type"),
]

_REQUEST_ADAPTER = TypeAdapter(Request)
_EVENT_ADAPTER = TypeAdapter(Event)


def parse_request(data: object) -> Request:
    """校验并构造请求信封；失败抛 pydantic.ValidationError。"""
    return _REQUEST_ADAPTER.validate_python(data)


def parse_event(data: object) -> Event:
    """校验并构造事件信封；失败抛 pydantic.ValidationError。"""
    return _EVENT_ADAPTER.validate_python(data)


def dump(obj: BaseModel) -> dict:
    """序列化为 JSON 友好 dict（ts 为 ISO 毫秒字符串）。"""
    return obj.model_dump(mode="json")
