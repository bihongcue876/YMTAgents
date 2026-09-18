"""信封与共享类型（spec §1.1 / §1.2）。

- `Envelope` 为基类，提供 `v/seq/ts`。
- 请求信封（UI → core）与事件信封（core → UI）均继承 `Envelope`。
- `Request` / `Event` 为带 `type` 判别器的联合类型，供 `parse_request` / `parse_event` 校验。
- 本模块只依赖标准库与 pydantic，禁止 import 项目内其它包。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union, get_args

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
    local: bool = False  # 本地模型服务：免密钥（spec rev10 §1）


class SessionMeta(BaseModel):
    id: str
    title: str
    persona_id: str | None = None  # rev23：会话所用角色（None = YMT 预置兜底）
    persona_name: str | None = None  # 展示用名称，由 controller 在推送索引时解析
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
    persona_id: str | None = None  # rev23：None = 全局默认角色


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


class FetchModels(Envelope):
    """拉取供应商端点自报的模型 ID 列表（spec rev9 §2）。

    用途：模型导入免手填 —— 用户不必凭空知道模型 ID，点一下即从端点取回候选。
    取回的是**候选**，是否登记仍由用户决定（不自动写配置）。
    """

    type: Literal["provider.models"] = "provider.models"
    provider_id: str


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


class ProviderModels(Envelope):
    """`provider.models` 的结果（spec rev9 §2）：端点自报的模型 ID 列表。"""

    type: Literal["provider.models.result"] = "provider.models.result"
    provider_id: str
    ok: bool
    models: list[str] = Field(default_factory=list)
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
# Persona（阶段 2 · spec rev23）：全局配置形同模型，会话各自选择
# ---------------------------------------------------------------------------
class PersonaInfo(BaseModel):
    id: str
    name: str
    builtin: bool = False  # YMT 预置：可编辑不可删
    prompt: str = ""  # prompt.md 全文（编辑器直接用）
    is_default: bool = False  # 全局默认（新会话用它）
    in_session: bool = False  # 当前会话正使用该角色


class PersonaList(Envelope):
    type: Literal["persona.list"] = "persona.list"
    personas: list[PersonaInfo] = Field(default_factory=list)


class PersonaSave(Envelope):
    """新建（无 id）或更新（有 id）角色。"""

    type: Literal["persona.save"] = "persona.save"
    persona_id: str | None = None
    name: str
    prompt: str


class PersonaDelete(Envelope):
    type: Literal["persona.delete"] = "persona.delete"
    persona_id: str


class PersonaSetDefault(Envelope):
    """设为全局默认角色（新会话用它）；等同模型的「上次使用」。"""

    type: Literal["persona.set_default"] = "persona.set_default"
    persona_id: str


class PersonaSwitch(Envelope):
    """当前会话切换角色（会话级，形同 model.switch；不同会话可各用各的）。"""

    type: Literal["persona.switch"] = "persona.switch"
    persona_id: str


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
        FetchModels,
        SettingsUpdate,
        PersonaList,
        PersonaSave,
        PersonaDelete,
        PersonaSetDefault,
        PersonaSwitch,
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
        ProviderModels,
        HealthReport,
        ErrorReport,
        SettingsState,
        PersonaList,
    ],
    Field(discriminator="type"),
]

# 联合成员类型元组：供边界层做**实例类型守卫**（不入队的对象必须回 invalid_request，
# 而不是被静默丢弃，spec rev9 §1）。由上面的联合派生，避免两处清单漂移。
REQUEST_MODELS: tuple[type[BaseModel], ...] = get_args(get_args(Request)[0])
EVENT_MODELS: tuple[type[BaseModel], ...] = get_args(get_args(Event)[0])

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
