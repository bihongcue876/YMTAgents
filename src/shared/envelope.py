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
    # rev25：生成阶段计时（平均 TPS 的唯一数据源，随事件落盘、回放一致）。
    # elapsed_ms = 请求发出到流结束；first_token_ms = 请求发出到首个 token（含预填充）。
    elapsed_ms: int = 0
    first_token_ms: int = 0


class ModelSpec(BaseModel):
    id: str
    ctx_window: int = 0  # 0 = 未知
    tags: list[str] = Field(default_factory=list)
    # rev25：思考能力 —— 用户偏好 + 自动探测结果（见 shared.schema.ModelConfig）。
    reasoning: Literal["auto", "on", "off"] = "auto"
    reasoning_detected: Literal["unknown", "yes", "no"] = "unknown"


class ProviderSpec(BaseModel):
    """下发到 GUI 的供应商视图；永不携带密钥明文（只带 key_status）。"""

    id: str
    name: str
    base_url: str
    models: list[ModelSpec] = Field(default_factory=list)
    key_status: Literal["stored", "missing", "error"] = "missing"
    local: bool = False  # 本地模型服务：免密钥（spec rev10 §1）


class SessionParams(BaseModel):
    """会话级模型参数（rev24）。字段为 `None` = 不下发、沿用供应商默认。

    界面上每个参数配一个启用开关：关 = None，开 = 有值。只发送用户显式启用的参数，
    避免把应用默认值悄悄覆盖到供应商侧（不同供应商默认不同）。
    """

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None


class SessionMeta(BaseModel):
    id: str
    title: str
    persona_id: str | None = None  # rev23：会话所用角色（None = YMT 预置兜底）
    persona_name: str | None = None  # 展示用名称，由 controller 在推送索引时解析
    created_at: datetime
    updated_at: datetime
    state: Literal["active", "archived"] = "active"
    main_model: str | None = None
    # rev24：会话级上下文与模型参数（随会话走，不落全局配置）
    max_context: int | None = None  # 输入侧上下文上限（token）；None = 用模型窗口
    note: str | None = None  # 作用/备注，纯展示
    params: SessionParams = Field(default_factory=SessionParams)
    # v0.0.1：本会话记忆开关（None = 用 config/memory.json 默认）
    memory_use: bool | None = None
    memory_compress: bool | None = None
    memory_auto: bool | None = None
    memory_threshold: int | None = None


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


class SessionDetail(Envelope):
    """请求某会话的详情（rev24）：计数、体积、最近一次上下文用量。"""

    type: Literal["session.detail"] = "session.detail"
    session_id: str


class SessionUpdate(Envelope):
    """整态更新会话可编辑字段（rev24）：面板提交的是**完整**期望状态。

    `title` 必填非空；`max_context=None` 表示「自动 = 用模型窗口」。
    """

    type: Literal["session.update"] = "session.update"
    session_id: str
    title: str
    note: str = ""
    max_context: int | None = None
    params: SessionParams = Field(default_factory=SessionParams)
    memory_use: bool | None = None  # v0.0.1：None = 用全局默认
    memory_compress: bool | None = None
    memory_auto: bool | None = None
    memory_threshold: int | None = None


class CompressMemory(Envelope):
    """压缩/概括当前会话记忆（v0.0.1）。

    只概括「较早、且未被上次记忆覆盖」的部分；近段按 token 预算保留（`keep_ratio`）。
    这是一次独立的模型调用，调用前会预告成本。
    """

    type: Literal["session.compress"] = "session.compress"
    session_id: str
    force: bool = False  # 预留：True 时忽略阈值（当前按钮触发即视为显式）


class BranchSession(Envelope):
    """从某条消息处**分支**（rev31）：新分支引用到 `from_seq`（含）为止的前缀。"""

    type: Literal["session.branch"] = "session.branch"
    session_id: str
    from_seq: int


class RevertSession(Envelope):
    """**退回到此前**（rev31）：把活动分支游标移到 `to_seq` 所在一轮之前。

    尾部事件 seq 保留在分支中（可切回，或再「从此处分支」）；events.jsonl 只增不改。
    """

    type: Literal["session.revert"] = "session.revert"
    session_id: str
    to_seq: int


class SwitchBranch(Envelope):
    """切换活动分支（rev31）：`session.events` 与后续装配均以活动分支的游标为准。"""

    type: Literal["session.switch_branch"] = "session.switch_branch"
    session_id: str
    branch_id: str


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
    section: Literal["network", "logging", "ui"] = "ui"
    data: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 事件信封（core → UI，spec §1.1）
# ---------------------------------------------------------------------------
class AssistantDelta(Envelope):
    type: Literal["msg.assistant.delta"] = "msg.assistant.delta"
    content: str
    turn_seq: int = 0
    reasoning: bool = False  # rev25：本段增量属于思考过程（非正文）


class AssistantFinal(Envelope):
    type: Literal["msg.assistant.final"] = "msg.assistant.final"
    content: str
    turn_seq: int = 0
    usage: Usage = Field(default_factory=Usage)
    interrupted: bool = False
    truncated: bool = False
    reasoning: str = ""  # rev25：完整思考过程（回放重建折叠块，不依赖 delta 事件）


class TurnStatus(Envelope):
    type: Literal["turn.status"] = "turn.status"
    turn_seq: int = 0
    state: Literal[
        "assembling", "probing", "summarizing", "calling", "done", "failed", "interrupted"
    ] = "assembling"
    error: str | None = None
    note: str | None = None  # rev25：瞬态用户提示（如探测思考能力的成本预告）


class ContextUsage(Envelope):
    type: Literal["ctx.usage"] = "ctx.usage"
    segments: dict[
        Literal[
            "system", "memory", "summary", "env", "files", "retrieve", "history", "reserve"
        ],
        int,
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


class SessionDetailResult(Envelope):
    """`session.detail` 的结果（rev24），也是面板刷新后的回推。"""

    type: Literal["session.detail.result"] = "session.detail.result"
    session_id: str
    meta: SessionMeta
    turn_count: int = 0
    user_count: int = 0
    assistant_count: int = 0
    data_bytes: int = 0
    effective_window: int = 0  # 实际生效窗口（会话 max_context 优先，否则模型窗口）
    last_usage: ContextUsage | None = None
    cumulative_tokens: int = 0  # 本会话各次调用 total 之和（用户裁决：区分单次 / 累计）
    # v0.0.1：会话记忆状态（无记忆时 revision=0、covered_seq=-1）
    memory_revision: int = 0
    memory_covered_seq: int = -1
    memory_tokens: int = 0
    memory_use: bool = True
    memory_compress: bool = True
    memory_auto: bool = False
    memory_threshold: int = 0
    memory_recommended_min: int = 0
    memory_recommended_max: int = 0
    memory_history: list[int] = Field(default_factory=list)  # 旧记忆归档 revision（可查看不注入）
    # rev31：分支树状态（活动分支作用域）。
    branch_count: int = 0
    active_branch: str = ""
    max_branches: int = 5


class SessionMemoryResult(Envelope):
    """`session.compress` 的结果（v0.0.1）。失败时 ok=False 且 error 为可读文案。"""

    type: Literal["session.memory.result"] = "session.memory.result"
    session_id: str
    ok: bool = True
    revision: int = 0
    covered_seq: int = -1
    tokens_before: int = 0  # 被覆盖历史部分的估算 token
    tokens_after: int = 0  # 记忆正文的估算 token（替代被覆盖部分）
    memory_tokens: int = 0  # 记忆正文估算 token
    recommended_min: int = 0  # 推荐范围下限（按 target_ratio 推导）
    recommended_max: int = 0  # 推荐范围上限（按 target_ratio 推导）
    model: str | None = None
    error: str | None = None


class BranchInfo(BaseModel):
    """分支概览（rev31）：供右栏「分支」段展示与切换。"""

    id: str
    parent: str | None = None
    fork_seq: int = -1
    created_at: datetime | None = None
    turns: int = 0  # 活动前缀内的用户轮数
    head_seq: int = -1  # 活动前缀最后一个事件 seq；-1 = 空
    active: bool = False


class SessionBranches(Envelope):
    """分支树回推（rev31）：`session.branch` / `revert` / `switch_branch` 后刷新。"""

    type: Literal["session.branches"] = "session.branches"
    session_id: str
    branches: list[BranchInfo] = Field(default_factory=list)
    active: str = "br0"
    max_branches: int = 5


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


class PersonaExport(Envelope):
    """把角色导出为单文件（rev32）；路径由 GUI 选择，**写盘在 core**（单写者）。"""

    type: Literal["persona.export"] = "persona.export"
    persona_id: str
    path: str


class PersonaImport(Envelope):
    """从单文件新建角色（rev32）；不覆盖既有，重名自动加后缀。"""

    type: Literal["persona.import"] = "persona.import"
    path: str


class PersonaExported(Envelope):
    type: Literal["persona.export.result"] = "persona.export.result"
    persona_id: str
    ok: bool
    path: str = ""
    name: str = ""
    error: str | None = None


class PersonaImported(Envelope):
    type: Literal["persona.import.result"] = "persona.import.result"
    ok: bool
    path: str = ""
    persona_id: str | None = None
    name: str = ""
    error: str | None = None


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
        SessionDetail,
        SessionUpdate,
        CompressMemory,
        BranchSession,
        RevertSession,
        SwitchBranch,
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
        PersonaExport,
        PersonaImport,
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
        SessionDetailResult,
        SessionMemoryResult,
        SessionBranches,
        ProviderList,
        TestResult,
        ProviderModels,
        HealthReport,
        ErrorReport,
        SettingsState,
        PersonaList,
        PersonaExported,
        PersonaImported,
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
