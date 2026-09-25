"""信封与共享类型（spec §1.1 / §1.2）。

- `Envelope` 为基类，提供 `v/seq/ts`。
- 请求信封（UI → core）与事件信封（core → UI）均继承 `Envelope`。
- `Request` / `Event` 为带 `type` 判别器的联合类型，供 `parse_request` / `parse_event` 校验。
- 本模块只依赖标准库与 pydantic，禁止 import 项目内其它包。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union, get_args

from pydantic import BaseModel, Field, TypeAdapter, field_serializer, model_validator


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
    enabled: bool = True  # rev68：供应商启停（禁用 = 模型不再可解析，槽位保留可逆）


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
    # v0.0.6：本会话所属工作区；None = 默认工作区（存量会话**零迁移** —— 缺字段即 None）
    workspace_id: str | None = None
    # rev59：是否已被用户手动重命名/保存（手动命名优先；True 后不再被自动标题覆盖）
    title_manual: bool = False


# ---------------------------------------------------------------------------
# 请求信封（UI → core，spec §1.1）
# ---------------------------------------------------------------------------
class SendMessage(Envelope):
    type: Literal["msg.user"] = "msg.user"
    text: str = Field(max_length=100_000)
    attachments: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=20
    )  # 相对当前工作区 root；核心侧另回退 default workspace/files


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
    #: v0.0.6：指定所属工作区（None = 当前工作区；当前为默认工作区时记 None 不记 id）
    workspace_id: str | None = None


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


class ProviderToggle(Envelope):
    """供应商启用/禁用（rev68；用户裁决）：禁用 = 其模型不再可解析，槽位保留可逆。"""

    type: Literal["provider.toggle"] = "provider.toggle"
    provider_id: str
    enabled: bool


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
        "assembling", "probing", "summarizing", "calling", "gating", "executing",
        "done", "failed", "interrupted"
    ] = "assembling"
    error: str | None = None
    note: str | None = None  # rev25：瞬态用户提示（如探测思考能力的成本预告）


class ContextUsage(Envelope):
    type: Literal["ctx.usage"] = "ctx.usage"
    segments: dict[
        Literal[
            "system", "memory", "summary", "env", "files", "tools", "retrieve", "history", "reserve"
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


class SessionTitleUpdated(Envelope):
    """rev59：会话标题已（异步）更新。GUI 据此刷新侧栏/会话列表/详情，不改协议请求。"""

    type: Literal["session.title_updated"] = "session.title_updated"
    session_id: str
    workspace_id: str | None = None
    branch_id: str | None = None
    title: str


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
    scope: Literal["session", "config", "gateway", "system", "library"] = "system"
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


# ---------------------------------------------------------------------------
# MCP 服务器管理 + 关卡（v0.0.3）
# ---------------------------------------------------------------------------
class McpServerUpsert(Envelope):
    """新增或更新 MCP server 配置。server 字段经 controller 校验为 McpServerConfig。"""

    type: Literal["mcp.server.upsert"] = "mcp.server.upsert"
    server: dict


class McpServerDelete(Envelope):
    type: Literal["mcp.server.delete"] = "mcp.server.delete"
    id: str


class McpServerToggle(Envelope):
    type: Literal["mcp.server.toggle"] = "mcp.server.toggle"
    id: str
    enabled: bool


class McpServerReconnect(Envelope):
    type: Literal["mcp.server.reconnect"] = "mcp.server.reconnect"
    id: str


class McpServersRefresh(Envelope):
    """请求重发 MCP 服务器列表与工具清单（插件页「刷新」）。"""

    type: Literal["mcp.server.refresh"] = "mcp.server.refresh"


class BuiltinToolToggle(Envelope):
    """v0.0.11（D-1）：内置工具开关（plugins.json -> builtin；关 = 真卸载）。"""

    type: Literal["tool.builtin.toggle"] = "tool.builtin.toggle"
    name: str
    enabled: bool


class McpScan(Envelope):
    """手动触发一次 MCP 服务器安全体检（v0.0.9）。

    只读展示：findings 永不参与权限裁定、可见性或工具派发（docs 07 §5）。
    checks 为空表示执行全部检测项（A1–A4 + B1–B4）。
    """

    type: Literal["mcp.scan"] = "mcp.scan"
    server_id: str
    checks: list[str] | None = None


class GateRespond(Envelope):
    """用户对关卡确认卡片的响应（allow/deny）。"""

    type: Literal["gate.respond"] = "gate.respond"
    call_id: str
    decision: Literal["allow", "deny"]


# ---------------------------------------------------------------------------
# Skills（v0.0.4）：Skill = 带元数据的纯提示词指令包（docs 07 §4.2）
# ---------------------------------------------------------------------------
class SkillToggle(Envelope):
    type: Literal["skill.toggle"] = "skill.toggle"
    id: str
    enabled: bool


class SkillImport(Envelope):
    """导入技能：目录（含 SKILL.md，Claude skill 风格连 resources/ 复制）、
    SKILL.md 文件路径、git 仓库 URL 或本地 git 目录（多技能仓库逐个发现导入）。"""

    type: Literal["skill.import"] = "skill.import"
    source: str


class SkillUpdate(Envelope):
    """按 skill.origin.json 记录的来源重新拉取覆盖（保 id 与启用态）。"""

    type: Literal["skill.update"] = "skill.update"
    id: str


class SkillDelete(Envelope):
    type: Literal["skill.delete"] = "skill.delete"
    id: str


class SkillPermission(Envelope):
    """逐技能权限档覆盖（落 plugins.json + audit；降权随时，提权=落盘显式）。"""

    type: Literal["skill.permission"] = "skill.permission"
    id: str
    permission: Literal["safe", "confirm", "restricted"]


class SkillsRefresh(Envelope):
    """请求重发技能列表（技能页「刷新」）。"""

    type: Literal["skill.refresh"] = "skill.refresh"

# ---------------------------------------------------------------------------
# Retrieval（检索模块，spec-2026-09-25-retrieval）：管理面请求
# ---------------------------------------------------------------------------
class RetrievalRefresh(Envelope):
    """重读检索配置并重发 retrieval.state（文件即配置，手工编辑 modules.json 后生效）。"""

    type: Literal["retrieval.refresh"] = "retrieval.refresh"


class RetrievalConfigUpdate(Envelope):
    """整态更新检索引擎开关与默认序（界面提交完整期望状态）。

    engines 键 = 引擎名（宿主常量集合，未知键 → invalid_request）；
    default_engines 含未启用项时该项被跳过（spec §5）。
    """

    type: Literal["retrieval.config.update"] = "retrieval.config.update"
    engines: dict[str, bool] | None = None
    default_engines: list[str] | None = Field(default=None, max_length=16)


class RetrievalKeySet(Envelope):
    """设置/清除某引擎的 API key（值立即进 Vault；请求信封不落盘，值过 bus 内存一次即弃）。"""

    type: Literal["retrieval.key.set"] = "retrieval.key.set"
    engine: Literal["webfetch", "duckduckgo", "baidu", "bing", "arxiv", "exa", "tavily"]
    #: None = 清除已存密钥；上限 4096 字符（超长即非法，防滥用）。
    value: str | None = Field(default=None, max_length=4096)


class RetrievalTest(Envelope):
    """手动触发单引擎节点安全检测（R1–R5，spec §10）；结果只读展示。"""

    type: Literal["retrieval.test"] = "retrieval.test"
    engine: Literal["webfetch", "duckduckgo", "baidu", "bing", "arxiv", "exa", "tavily"]


# ---------------------------------------------------------------------------
# Shell（v0.0.5）：本地 shell 宿主（docs 07 §4.1 / 09 §2）
# ---------------------------------------------------------------------------
class ShellSpawn(Envelope):
    """用户手动新建一个本地终端（docs 01 §6④「用户可手动使用」）。

    与模型侧 `shell.exec(new=True)` 共用同一池与上限；`cwd=None` 用配置的初始目录。
    """

    type: Literal["shell.spawn"] = "shell.spawn"
    cwd: str | None = None


class ShellClose(Envelope):
    """关闭指定 shell（终止其进程并回收其注册痕迹）。"""

    type: Literal["shell.close"] = "shell.close"
    id: str


class ShellInput(Envelope):
    """用户在终端页直接把命令送进某个 shell。

    用户即主决策者 → 不经确认关卡（docs 09 §3），但记 audit（与白名单编辑同理）。
    """

    type: Literal["shell.input"] = "shell.input"
    id: str
    command: str


class ShellRefresh(Envelope):
    """请求重发 shell 列表与权限档（终端页「刷新」）。"""

    type: Literal["shell.refresh"] = "shell.refresh"


# ---------------------------------------------------------------------------
# 工作区（v0.0.6）：默认工作区 + 可折叠的自建工作区（spec v0.0.6）
# ---------------------------------------------------------------------------
class WorkspaceCreate(Envelope):
    """新建工作区。

    - `root_kind="managed"`：留空 `root`，由宿主在 `ymtdata/workspaces/<id>/` 下分配；
    - `root_kind="external"`：`root` 必填为**已存在的绝对目录**（用户显式选择 + 知情确认）。
    """

    type: Literal["workspace.create"] = "workspace.create"
    name: str
    root_kind: Literal["managed", "external"] = "managed"
    root: str | None = None
    #: None = 用 `settings.json → workspace.default_data_home_kind`（出厂默认 `inline`）
    data_home_kind: Literal["inline", "managed", "custom"] | None = None
    data_home: str | None = None
    #: 构建命令（切片 4 才执行，本期只保存与展示）。**建时就要能填** ——
    #: 界面已经提供了输入框，契约若不收就会被静默丢掉。
    build_cmd: str = ""
    note: str | None = None


class WorkspaceUpdate(Envelope):
    """整态更新工作区（同 `session.update` 先例：面板提交**完整**期望状态）。

    `root` / `data_home_kind` / `data_home` 为 None = **不变**。更换目录与落点是不可逆的
    用户动作，必须由界面显式提交新值，不因「整态提交」被意外清空。
    """

    type: Literal["workspace.update"] = "workspace.update"
    id: str
    name: str
    note: str = ""
    build_cmd: str = ""
    root: str | None = None
    data_home_kind: Literal["inline", "managed", "custom"] | None = None
    data_home: str | None = None


class WorkspaceDelete(Envelope):
    """移除工作区登记（**不删磁盘上的任何文件**，spec v0.0.6 §3.13 R2）。"""

    type: Literal["workspace.delete"] = "workspace.delete"
    id: str


class WorkspaceSwitch(Envelope):
    """切换当前工作区（运行态，不落盘）：新会话默认进它。"""

    type: Literal["workspace.switch"] = "workspace.switch"
    id: str


class WorkspaceRefresh(Envelope):
    """重读登记表（文件即配置：手工编辑 `workspaces/index.json` 后刷新即生效）。"""

    type: Literal["workspace.refresh"] = "workspace.refresh"


class WorkspaceDetail(Envelope):
    """取单个工作区的详情（含**有界**的文件列表）；`path` 为相对 root 的子路径。"""

    type: Literal["workspace.detail"] = "workspace.detail"
    id: str
    path: str = ""


class WorkspaceMemoryWrite(Envelope):
    """显式写入工作区级 AGENTS.md；不提供 Agent 自主写入通道。"""

    type: Literal["workspace.memory_write"] = "workspace.memory_write"
    scope: Literal["current", "default", "specific"] = "current"
    workspace_id: str | None = Field(default=None, max_length=64)
    mode: Literal["append", "replace"] = "append"
    text: str = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def _specific_requires_id(self) -> WorkspaceMemoryWrite:
        if self.scope == "specific" and not self.workspace_id:
            raise ValueError("scope=specific 时必须提供 workspace_id。")
        return self


class WorkspaceBuild(Envelope):
    """执行已登记构建命令；UI 负责逐次展示完整命令并取得本次显式点击。"""

    type: Literal["workspace.build"] = "workspace.build"
    id: str = Field(min_length=1, max_length=64)


class WorkspaceFileRead(Envelope):
    """读取工作区内一个文本文件（GUI 编辑器用；不做二进制猜测）。"""

    type: Literal["workspace.file_read"] = "workspace.file_read"
    id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=512)


class WorkspaceFileWrite(Envelope):
    """写回工作区内一个文本文件（GUI 编辑器用；原子写 + 一代备份）。

    仅经界面显式触发：不注册为模型工具、不新增 agent 写通道（能力面零扩张）。
    """

    type: Literal["workspace.file_write"] = "workspace.file_write"
    id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=262_144)
    create: bool = False


class MoveSession(Envelope):
    """把会话挪到另一工作区（`workspace_id=None` = 默认工作区）。"""

    type: Literal["session.move"] = "session.move"
    session_id: str
    workspace_id: str | None = None


# ---------------------------------------------------------------------------
# 小图书馆（DPIM）：管理请求只由核心线程落盘，Agent 查询工具只读。
# ---------------------------------------------------------------------------
class LibraryCreate(Envelope):
    type: Literal["library.create"] = "library.create"
    name: str = Field(min_length=1, max_length=80)
    root_kind: Literal["managed", "external"] = "managed"
    root: str | None = Field(default=None, max_length=4096)
    group: str | None = Field(default=None, max_length=60)
    model_ref: str = Field(default="main", min_length=1, max_length=200)
    note: str = Field(default="", max_length=1000)


class LibraryUpdate(Envelope):
    type: Literal["library.update"] = "library.update"
    id: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    root: str | None = Field(default=None, max_length=4096)
    group: str | None = Field(default=None, max_length=60)
    model_ref: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class LibraryDelete(Envelope):
    """移除登记，不删除库文件。"""

    type: Literal["library.delete"] = "library.delete"
    id: str = Field(min_length=1, max_length=64)


class LibrarySwitch(Envelope):
    type: Literal["library.switch"] = "library.switch"
    id: str = Field(min_length=1, max_length=64)


class LibraryRefresh(Envelope):
    """重读登记表；id 指定时只修复该库派生图，否则重读索引。"""

    type: Literal["library.refresh"] = "library.refresh"
    id: str | None = Field(default=None, max_length=64)


class LibraryDetail(Envelope):
    type: Literal["library.detail"] = "library.detail"
    id: str = Field(min_length=1, max_length=64)
    event_offset: int = Field(default=0, ge=0, le=10_000_000)
    event_limit: int = Field(default=50, ge=1, le=200)
    graph_limit: int = Field(default=300, ge=1, le=1000)
    graph_library_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=20
    )
    focus_event_id: str | None = Field(default=None, max_length=64)


class LibraryIngest(Envelope):
    type: Literal["library.ingest"] = "library.ingest"
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=100_000)
    event_type: Literal["interaction", "data", "source"] = "interaction"
    index: bool = True
    #: 指定时重试或跳过既有事件；不再追加新原文。
    event_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _content_or_existing_event(self) -> LibraryIngest:
        if bool(self.event_id) == bool(self.text.strip()):
            raise ValueError("必须且只能提供新文本或既有 event_id。")
        return self


class LibraryQuery(Envelope):
    type: Literal["library.query"] = "library.query"
    lib_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=100
    )
    query: str = Field(min_length=1, max_length=4000)
    mode: Literal["hybrid", "events", "nodes"] = "hybrid"
    top_k: int = Field(default=8, ge=1, le=50)


# ---------------------------------------------------------------------------
# 附加功能总开关（切片 0，用户 2026-09-24 裁决）
# ---------------------------------------------------------------------------
class FeatureToggle(Envelope):
    """二态滑动开关：`关` = 真卸载（不 import/不注册/无后台/释放引用），`开` = 惰性装配。"""

    type: Literal["feature.toggle"] = "feature.toggle"
    name: str
    enabled: bool


class FeatureState(Envelope):
    """附加功能态推送：每项 `{name, enabled, state, available}`。

    `available=False` 表示 schema 中已声明、但当前没有可装配宿主；界面仍可展示，不能启用。
    唯一启停真值仍是 `modules.json → features`。
    """

    type: Literal["feature.state"] = "feature.state"
    features: list[dict] = Field(default_factory=list)


class LibraryList(Envelope):
    type: Literal["library.list"] = "library.list"
    libraries: list[dict] = Field(default_factory=list)
    current: str | None = None
    error: str = ""


class LibraryDetailResult(Envelope):
    type: Literal["library.detail.result"] = "library.detail.result"
    id: str
    root: str = ""
    counts: dict = Field(default_factory=dict)
    events: list[dict] = Field(default_factory=list)
    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)
    event_offset: int = 0
    event_limit: int = 50
    truncated: bool = False
    error: str = ""


class LibraryIngestResult(Envelope):
    type: Literal["library.ingest.result"] = "library.ingest.result"
    id: str
    event_id: str = ""
    status: Literal["raw", "indexed", "linked", "failed", "skipped"] = "raw"
    indexed: bool = False
    message: str = ""


class LibraryQueryResult(Envelope):
    type: Literal["library.query.result"] = "library.query.result"
    query: str
    library_ids: list[str] = Field(default_factory=list)
    results: list[dict] = Field(default_factory=list)
    debug: list[dict] = Field(default_factory=list)


class LibraryGraphResult(Envelope):
    type: Literal["library.graph.result"] = "library.graph.result"
    library_ids: list[str] = Field(default_factory=list)
    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------------------
# 副思考链（BTCM，切片 2/3）
# ---------------------------------------------------------------------------
class BtcmUpdate(Envelope):
    """更新副思考链子选项（开启后的手动/自动、指定槽位）。"""

    type: Literal["btcm.update"] = "btcm.update"
    trigger: Literal["manual", "auto"] | None = None
    slot: str | None = None


class BtcmState(Envelope):
    """副思考链态推送（子选项 + 就绪）。宿主开关态另由 `feature.state` 呈现。"""

    type: Literal["btcm.state"] = "btcm.state"
    trigger: Literal["manual", "auto"] = "manual"
    slot: str = "thinking"
    ready: bool = False


class BtcmRun(Envelope):
    """副思考链页「运行一次」：走与 `btcm.think` 相同的工具路径（不另开通道）。"""

    type: Literal["btcm.run"] = "btcm.run"
    question: str
    effort: Literal["light", "standard", "deep"] = "standard"
    mode: Literal["auto", "creative", "validate", "long"] = "auto"


class ThinkDelta(Envelope):
    """思考流增量（对 Agent 循环不作为决策输入，docs 06 §7）。"""

    type: Literal["think.delta"] = "think.delta"
    think_id: str = ""
    agent: str = ""
    kind: Literal["reasoning", "content"] = "content"
    text: str = ""


class ThinkIteration(Envelope):
    """思考流按轮摘要（verdict / meta decision）。"""

    type: Literal["think.iteration"] = "think.iteration"
    think_id: str = ""
    iteration: int = 0
    verdict: str | None = None
    decision: str | None = None


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
# MCP + 工具调用事件（v0.0.3）
# ---------------------------------------------------------------------------
class McpServerList(Envelope):
    """MCP 服务器列表（推给插件页）。"""

    type: Literal["mcp.server.list"] = "mcp.server.list"
    servers: list[dict] = Field(default_factory=list)


class McpServerStatus(Envelope):
    type: Literal["mcp.server.status"] = "mcp.server.status"
    id: str
    state: str = "stopped"
    tools: list[str] = Field(default_factory=list)
    error: str | None = None


class ToolList(Envelope):
    """已注册工具列表（插件页展示）。"""

    type: Literal["tool.list"] = "tool.list"
    tools: list[dict] = Field(default_factory=list)


class ToolCall(Envelope):
    """模型发起工具调用（落 events.jsonl + 推 UI 聊天流）。"""

    type: Literal["tool.call"] = "tool.call"
    call_id: str
    name: str
    args: dict = Field(default_factory=dict)
    permission: str = "confirm"


class ToolResult(Envelope):
    """工具执行结果（落 events.jsonl + 推 UI 聊天流）。"""

    type: Literal["tool.result"] = "tool.result"
    call_id: str
    ok: bool = True
    output: str | None = None
    output_ref: str | None = None
    usage: dict | None = None
    duration_ms: int = 0
    error: dict | None = None


class GateRequest(Envelope):
    """关卡确认请求（推 UI 确认卡片；含参数原文供用户审视）。"""

    type: Literal["gate.request"] = "gate.request"
    call_id: str
    name: str
    args: dict = Field(default_factory=dict)
    permission: str = "confirm"
    #: v0.0.11（对照模式）：只给 UI 看的**预览**（如写族的 old / new 文本），
    #: 由 ToolSpec.preview 生成；**不进 tool.call 事件**，不影响模型可见内容。
    preview: dict = Field(default_factory=dict)


class GateResult(Envelope):
    """关卡确认结果（落 events.jsonl）。"""

    type: Literal["gate.result"] = "gate.result"
    call_id: str
    decision: str = "deny"
    decider: str = "policy"


class BuiltinToolState(Envelope):
    """v0.0.11（D-1）：内置工具清单与快照（items = {name,permission,enabled}；面板一次拉全量）。"""

    type: Literal["tool.builtin.state"] = "tool.builtin.state"
    items: list[dict] = Field(default_factory=list)


class McpScanResult(Envelope):
    """MCP 安全体检结果（v0.0.9；证据已脱敏，只读展示）。

    findings 元素为 {id,name,status,severity,evidence,suggestion}；
    summary 为 {pass,warn,fail,skip} 计数。
    """

    type: Literal["mcp.scan.result"] = "mcp.scan.result"
    server_id: str
    server_name: str = ""
    scanned_at: str = ""
    findings: list[dict] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Skills 事件（v0.0.4）
# ---------------------------------------------------------------------------
class SkillList(Envelope):
    """技能列表（推给技能页）。"""

    type: Literal["skill.list"] = "skill.list"
    skills: list[dict] = Field(default_factory=list)


class SkillImported(Envelope):
    """导入/更新结果回执（skill.update 复用本事件，updated=True）。"""

    type: Literal["skill.import.result"] = "skill.import.result"
    ok: bool
    source: str = ""
    skill_ids: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    updated: bool = False
    error: str | None = None

# ---------------------------------------------------------------------------
# Retrieval 事件（检索模块）
# ---------------------------------------------------------------------------
class RetrievalState(Envelope):
    """检索模块引擎态快照（**不落盘**：配置真值在 modules.json，运行态不入盘）。

    engines 元素 = {name, enabled, needs_key, key_state, endpoints}；
    key_state ∈ stored/missing/error（**绝不含 key 值**）。
    """

    type: Literal["retrieval.state"] = "retrieval.state"
    engines: list[dict] = Field(default_factory=list)
    #:工具级开关快照（rev68 界面优化）：{name, title, permission, enabled}（与内置工具面板同真值）。
    tools: list[dict] = Field(default_factory=list)
    default_engines: list[str] = Field(default_factory=list)
    module_state: str = "disabled"


class RetrievalTestResult(Envelope):
    """单引擎节点安全检测结果（spec §10；证据过 redact，只读展示，不参与权限/派发）。

    findings 元素 = {id, status, evidence, suggestion}（status ∈ pass/warn/fail/skip）。
    """

    type: Literal["retrieval.test.result"] = "retrieval.test.result"
    engine: str
    findings: list[dict] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    tested_at: str = ""


# ---------------------------------------------------------------------------
# Shell 事件（v0.0.5）
# ---------------------------------------------------------------------------
class ShellList(Envelope):
    """shell 列表 + 权限档 + 上限（推给终端页）。

    **不落盘**：shell 是运行态（docs 03 §8「一切运行态不入盘」）。
    一帧带全权限与上限，省一次往返。
    """

    type: Literal["shell.list"] = "shell.list"
    shells: list[dict] = Field(default_factory=list)
    max_shells: int = 5
    permission: str = "confirm"  # 有效权限档（覆盖 ⊕ 缺省）
    allow_restricted: bool = False  # 高危档是否已显式启用


class ShellOutput(Envelope):
    """某个 shell 的增量输出（监视用）。**不落盘**（运行态）。"""

    type: Literal["shell.output"] = "shell.output"
    id: str
    chunk: str = ""


# ---------------------------------------------------------------------------
# 工作区事件（v0.0.6）
# ---------------------------------------------------------------------------
class WorkspaceInfo(BaseModel):
    """工作区的**下发视图**（展示用）。

    与落盘形态 `WorkspaceRecord` 的区别：`root` / `data_home` 已解析为**物理绝对路径**，
    并附上派生的三个事实徽标 —— `migratable`（是否随数据根迁移）、`missing`（root 是否已消失）、
    `builtin`（是否默认工作区）。派生量在宿主侧算，界面不重复推导（单一来源）。
    """

    id: str
    name: str
    builtin: bool = False
    current: bool = False
    root_kind: Literal["managed", "external"] = "managed"
    data_home_kind: Literal["inline", "managed", "custom"] = "inline"
    root: str = ""
    data_home: str = ""
    migratable: bool = False
    missing: bool = False
    sessions: int = 0
    build_cmd: str = ""
    note: str | None = None


class WorkspaceList(Envelope):
    """工作区登记表快照 + 当前工作区 + 侧栏折叠态。

    `current` 与 `collapsed` 都是**运行态/UI 偏好**：前者不落盘，后者落 `settings.ui`。
    一帧带全，省去界面自行推导的往返。
    """

    type: Literal["workspace.list"] = "workspace.list"
    workspaces: list[WorkspaceInfo] = Field(default_factory=list)
    current: str = "ws_default"
    collapsed: list[str] = Field(default_factory=list)


class WorkspaceDetailResult(Envelope):
    """单个工作区的详情：有界文件列表 + 解析后的路径。

    `entries` 为 `{name, dir, size}` 三元组列表（相对 `path` 的一层）；超 `file_limit`
    时 `truncated=True`（**不静默截断**，界面据此提示）。
    """

    type: Literal["workspace.detail.result"] = "workspace.detail.result"
    id: str
    path: str = ""
    root: str = ""
    data_home: str = ""
    entries: list[dict] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None


class WorkspaceMemoryResult(Envelope):
    type: Literal["workspace.memory.result"] = "workspace.memory.result"
    workspace_id: str
    scope: Literal["current", "default", "specific"]
    mode: Literal["append", "replace"]
    chars: int = 0
    ok: bool = True
    error: str = ""


class WorkspaceBuildResult(Envelope):
    type: Literal["workspace.build.result"] = "workspace.build.result"
    id: str
    ok: bool
    exit_code: int | None = None
    duration_ms: int = 0
    output: str = Field(default="", max_length=250_000)
    log_path: str = ""
    started_at: str = ""


class WorkspaceFileResult(Envelope):
    type: Literal["workspace.file.result"] = "workspace.file.result"
    id: str
    path: str
    ok: bool = True
    is_write: bool = False
    content: str = Field(default="", max_length=262_144)
    bytes: int = 0
    truncated: bool = False
    error: str = ""


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
        ProviderToggle,
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
        McpServerUpsert,
        McpServerDelete,
        McpServerToggle,
        McpServerReconnect,
        McpServersRefresh,
        McpScan,
        GateRespond,
        SkillToggle,
        SkillImport,
        SkillUpdate,
        SkillDelete,
        SkillPermission,
        SkillsRefresh,
        ShellSpawn,
        ShellClose,
        ShellInput,
        ShellRefresh,
        WorkspaceCreate,
        WorkspaceUpdate,
        WorkspaceDelete,
        WorkspaceSwitch,
        WorkspaceRefresh,
        WorkspaceDetail,
WorkspaceMemoryWrite,
        WorkspaceBuild,
        WorkspaceFileRead,
        WorkspaceFileWrite,
        MoveSession,
        LibraryCreate,
        LibraryUpdate,
        LibraryDelete,
        LibrarySwitch,
        LibraryRefresh,
        LibraryDetail,
        LibraryIngest,
        LibraryQuery,
        FeatureToggle,
        BtcmUpdate,
        BtcmRun,
        BuiltinToolToggle,
        RetrievalRefresh,
        RetrievalConfigUpdate,
        RetrievalKeySet,
        RetrievalTest,
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
        SessionTitleUpdated,
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
        McpServerList,
        McpServerStatus,
        ToolList,
        ToolCall,
        ToolResult,
        GateRequest,
        GateResult,
        McpScanResult,
        SkillList,
        SkillImported,
        ShellList,
        ShellOutput,
        WorkspaceList,
        WorkspaceDetailResult,
WorkspaceMemoryResult,
        WorkspaceBuildResult,
        WorkspaceFileResult,
        LibraryList,
        LibraryDetailResult,
        LibraryIngestResult,
        LibraryQueryResult,
        LibraryGraphResult,
        FeatureState,
        BtcmState,
        ThinkDelta,
        ThinkIteration,
        BuiltinToolState,
        RetrievalState,
        RetrievalTestResult,
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
