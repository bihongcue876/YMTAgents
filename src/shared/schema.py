"""配置文件 schema（docs 03 §3）。

对应 `ymtdata/config/` 下的四个 JSON 文件，均带 `schema_version: 1`。
与 spec 的「下发视图」类型（ProviderSpec 等）不同：本模块是**落盘形态**，
供应商携带 `key_ref`（引用本地加密库），永不携带明文密钥。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# models.json
# ---------------------------------------------------------------------------
class ModelConfig(BaseModel):
    id: str
    ctx_window: int = 0  # 0 = 未知
    tags: list[str] = Field(default_factory=list)
    # rev25：思考（reasoning）。`reasoning` 是用户偏好（auto=按检测自动；on/off=人工覆盖，
    # 用于纠正探测误判）；`reasoning_detected` 是自动探测结果缓存（仅 auto 时生效）。
    reasoning: Literal["auto", "on", "off"] = "auto"
    reasoning_detected: Literal["unknown", "yes", "no"] = "unknown"
    # 该端点是否接受 `reasoning_effort` 参数（探测得知）。False 时即便模型会思考，
    # 也只能被动接收其回流，不能主动下发参数（否则某些模型会 400，rev25）。
    reasoning_param_ok: bool = False
    # rev42：该端点不接受 function calling（带 tools 的请求返回 400）。
    # 被动判定：某回合带工具被拒 → 撤工具重试一次并置 True，此后不再下发工具。
    tools_unsupported: bool = False


class ProviderConfig(BaseModel):
    id: str  # prv_<uuid7>
    name: str
    base_url: str
    key_ref: str | None = None  # vault://<prv_id>；None = 未设置密钥（v0.0.2：本地加密机密库）
    local: bool = False  # 本地模型服务：免密钥（spec rev10 §1）
    models: list[ModelConfig] = Field(default_factory=list)


class ModelsConfig(BaseModel):
    schema_version: Literal[1] = 1
    providers: list[ProviderConfig] = Field(default_factory=list)
    slots: dict[Literal["main", "thinking", "fast", "embedding"], str | None] = Field(
        default_factory=lambda: {
            "main": None,
            "thinking": None,
            "fast": None,
            "embedding": None,
        }
    )


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------
class NetworkSettings(BaseModel):
    whitelist: list[str] = Field(default_factory=list)  # 域名精确或 *.后缀


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"


class UISettings(BaseModel):
    """UI 选项。取值与 `gui.theme` 的 `PALETTES` / `FONT_LEVELS` 一致（docs 05 §1）。"""

    theme: Literal["light", "dark"] = "light"
    font_size: Literal["small", "normal", "large", "xlarge"] = "normal"
    #: v0.0.6：已折叠的工作区分组 id（侧栏 accordion 的折叠态）。
    #: 属 UI 偏好，与 theme/font_size 同族 —— 走既有 `settings.update(section="ui")`，
    #: **不新增请求/事件类型**；每一项是工作区 id（非名称，名称不参与任何状态键）。
    collapsed_workspaces: list[str] = Field(default_factory=list)
    #: v0.0.11（切片 E）：消息流内的复制按钮（复制 MD / 复制原文）是否显示。
    #: 与 theme/font_size 同族 UI 偏好 —— 走既有 `settings.update(section="ui")`，
    #: **不新增请求/事件类型**。
    copy_buttons: bool = True


# ---------------------------------------------------------------------------
# settings.json → workspace（v0.0.6：工作区的出厂默认；登记表在 workspaces/index.json）
# ---------------------------------------------------------------------------
class WorkspaceSettings(BaseModel):
    """工作区的全局出厂默认与展示边界。

    - `default_data_home_kind`：新建工作区的**默认**数据落点。用户裁决（2026-09-22 D3）：
      默认在工作区之中建 `.ymtdata` 这类私有数据目录 → 故为 `inline`。
    - `file_depth` / `file_limit`：文件列表的**有界**遍历（深度/条数），防越界遍历与卡死。

    - `build_timeout_ms`：单次用户确认构建的有界超时，默认 10 分钟。
    """

    default_data_home_kind: Literal["inline", "managed", "custom"] = "inline"
    file_depth: int = 2
    file_limit: int = 500
    build_timeout_ms: int = Field(default=600_000, ge=1_000, le=600_000)


class SettingsConfig(BaseModel):
    schema_version: Literal[1] = 1
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    ui: UISettings = Field(default_factory=UISettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    auto_title: bool = True  # rev59：是否在新会话首轮回复后自动生成标题（模型提炼）


# ---------------------------------------------------------------------------
# workspaces/index.json（v0.0.6：工作区登记表 —— 唯一事实源，可读、可 diff、可手编）
# ---------------------------------------------------------------------------
class WorkspaceRecord(BaseModel):
    """一个工作区的登记项。

    三个概念**正交**，互不混叠（spec v0.0.6 §3.1）：
    - `root`：真实目录 —— shell 的 cwd、文件列表的根、构建的执行位置；
    - `data_home`：该工作区私有的 `ymtdata` —— 记忆 `AGENTS.md`、文档 `documents/`、产物 `artifacts/`；
    - 本记录：`index.json` 里的一行。

    路径编码（`root` / `data_home` 两个字段，按各自的 `*_kind` 解释）：

    | kind | 字段含义 |
    |---|---|
    | `managed`（root）/ `managed`（data_home） | **相对数据根**的相对路径（可随数据根整体迁移） |
    | `external`（root）/ `custom`（data_home） | **绝对路径**（不可迁移，诚实标注） |
    | `inline`（data_home） | 固定 `.ymtdata`，相对 `root` 解析 |

    `name` **不参与任何路径构造**（id 才是路径）→ 名称里的 `..` / `/` / `:` 无注入面。
    """

    schema_version: Literal[1] = 1
    id: str
    name: str
    root_kind: Literal["managed", "external"] = "managed"
    root: str
    data_home_kind: Literal["inline", "managed", "custom"] = "inline"
    data_home: str = ".ymtdata"
    created_at: datetime
    build_cmd: str | None = None
    note: str | None = None


class WorkspaceIndex(BaseModel):
    """工作区登记表（`ymtdata/workspaces/index.json`）。

    `current` **不落盘** —— 它是运行态（与「运行态不入盘」纪律一致），
    此处不设字段；当前工作区由 `WorkspaceManager` 在内存中持有。
    """

    schema_version: Literal[1] = 1
    workspaces: list[WorkspaceRecord] = Field(default_factory=list)



# ---------------------------------------------------------------------------
# memory.json（v0.0.1：会话记忆（压缩）的出厂默认，阈值为用户可改的数值）
# ---------------------------------------------------------------------------
class MemoryConfig(BaseModel):
    """会话记忆（历史压缩）的全局出厂默认；按会话覆盖 `use`/`compress`/`threshold`。

    - `use`：是否把会话记忆注入上下文（默认开）。
    - `compress`：是否**允许**压缩（默认开）。
    - `auto`：是否**自动**在占用达阈值时压缩（默认关；需 `use & compress & auto` 三者皆真）。
    - `threshold`：占用百分比，**由用户指定**；占用达到该值才**允许**自动压缩，
      低于它绝不压（用户裁决 2026-09-19）。
    - `keep_ratio`：尾部保留 = 生效窗口 // keep_ratio（默认 1/8），保证近期对话不进记忆。
    - `target_ratio`：记忆目标按窗口比例推导**推荐范围**（max = 窗口 // target_ratio，
      min = max // 2）；**不设绝对目标、不硬钳窗口**，压缩以质量优先。
    - `model_slot`：压缩调用所用槽位；None = 用会话当前模型。
    """

    schema_version: Literal[1] = 1
    use: bool = True
    compress: bool = True
    auto: bool = False
    threshold: int = 90
    keep_ratio: int = 8
    target_ratio: int = 16
    model_slot: Literal["main", "thinking", "fast", "embedding"] | None = None


# ---------------------------------------------------------------------------
# sessions/<id>/branches/<bid>/memory.json（v0.0.1：会话记忆状态；正文在同目录 memory.md）
# ---------------------------------------------------------------------------
class SessionMemory(BaseModel):
    """一次记忆压缩的落盘状态（append-only 事实源 `events.jsonl` 不动）。"""

    schema_version: Literal[1] = 1
    revision: int = 0
    covered_seq: int = -1  # 已被记忆覆盖到的最后一个事件 seq；-1 = 尚未压缩
    model: str | None = None  # 生成该记忆的模型
    tokens_est: int = 0  # 记忆正文的估算 token（装配时占用的量）
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# sessions/<id>/graph.json（rev31：分支树；引用 events.jsonl 的 seq，绝不复制事件）
# ---------------------------------------------------------------------------
class BranchRecord(BaseModel):
    """一条分支：只记自己引用的事件 seq 与游标；事件本体始终在 events.jsonl，只增不改。

    - `events`：本分支按序引用的事件 seq（可含被回退的尾部，以便切换回来时仍在）。
    - `cursor`：活动前缀长度（`events[:cursor]` 才是当前对话）；回退只改游标，不删 seq。
    - `fork_seq`：分支起点 seq（br0 为 -1）；`parent`：父分支 id（br0 为 None）。
    """

    id: str
    parent: str | None = None
    fork_seq: int = -1
    created_at: datetime | None = None
    events: list[int] = Field(default_factory=list)
    cursor: int = 0


class SessionGraph(BaseModel):
    """会话的分支树（rev31）。br0 为主干；`active` 指向当前分支。"""

    schema_version: Literal[1] = 1
    active: str = "br0"
    branches: list[BranchRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# modules.json（期望态；运行态不入盘，docs 03 §3.3）
# ---------------------------------------------------------------------------
class DpimJointConfig(BaseModel):
    """默认联合检索范围；具体查询仍可由调用方显式限定。"""

    default_libs: list[str] = Field(default_factory=list, max_length=100)
    top_k: int = Field(default=8, ge=1, le=50)


class DpimConfig(BaseModel):
    joint: DpimJointConfig = Field(default_factory=DpimJointConfig)


class LibraryRecord(BaseModel):
    """小图书馆登记项；用户显示名与物理路径键严格分离。"""

    id: str = Field(pattern=r"^lib_[0-9a-fA-F-]{16,}$", max_length=64)
    name: str = Field(min_length=1, max_length=80)
    root_kind: Literal["managed", "external"] = "managed"
    #: managed 留空，物理位置由 id + group_key 推导；external 为绝对路径。
    root: str | None = Field(default=None, max_length=4096)
    group: str | None = Field(default=None, max_length=60)
    #: 独立生成的目录键，绝不由 group 显示名计算。
    group_key: str | None = Field(default=None, pattern=r"^grp_[0-9a-fA-F-]{16,}$", max_length=64)
    #: 槽位名（main/thinking/fast/embedding）或模型 ID；绝不存密钥。
    model_ref: str = Field(default="main", min_length=1, max_length=200)
    note: str = Field(default="", max_length=1000)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _root_and_group_pairs(self) -> LibraryRecord:
        if not self.name.strip():
            raise ValueError("书库名称不能为空。")
        if self.root_kind == "managed" and self.root is not None:
            raise ValueError("托管书库不保存自定义 root。")
        if self.root_kind == "external" and not self.root:
            raise ValueError("外部书库必须保存 root。")
        if self.group is None and self.group_key is not None:
            raise ValueError("未分组书库不应带 group_key。")
        return self


class LibraryIndex(BaseModel):
    """`ymtdata/libraries/index.json` 唯一事实源。"""

    schema_version: Literal[1] = 1
    libraries: list[LibraryRecord] = Field(default_factory=list, max_length=100)


class BtcmAgentParams(BaseModel):
    """BTCM 单个 Agent 的可调参数（均可缺省，缺省用宿主默认）。不下发 max_tokens（rev20）。"""

    temperature: float | None = None
    timeout: int | None = None
    num_candidates: int | None = None
    log_intermediate: bool | None = None


class BtcmConfig(BaseModel):
    """副思考链配置（切片 2）。

    宿主级启停由 `FeaturesConfig.btcm` 管（关=卸载）；此处是**开启后的子选项**：
    `trigger` 手动/自动（自动=环境声明挂一条「遇严重矛盾可发起一次中级思考」策略）。
    """

    #: `off` 为存量兼容值（旧配置）；宿主级启停改由 `FeaturesConfig.btcm` 决定，
    #: 读取时一律按 `manual` 处理（见 BtcmManager.state_payload）。
    trigger: Literal["manual", "auto", "off"] = "manual"
    slot: Literal["main", "thinking", "fast", "embedding"] = "thinking"
    max_iterations: int = 2
    timeout: int = 3600
    enable_creative: bool = True
    enable_validator: bool = True
    agents: dict[str, BtcmAgentParams] = Field(default_factory=dict)


class FeaturesConfig(BaseModel):
    """附加功能总开关（切片 0，用户 2026-09-24 裁决）——**宿主启停的唯一真值**。

    - 每个附加功能一个二态滑动开关；`关` = 真卸载（不 import、不注册、无后台活动、
      释放引用），`开` = 惰性装配（六条硬指标，见 spec）。
    - 默认保真：`mcp`/`shell`/`skills` 沿用既有「默认装载」行为；`btcm`/`dpim` 未启用默认关。
    - 模块内部档位（`BtcmConfig.trigger`、各 server `enabled` 等）是开启后的**子选项**，
      不参与宿主级启停。
    """

    mcp: bool = True
    shell: bool = True
    skills: bool = True
    btcm: bool = False
    dpim: bool = False


class ShellConfig(BaseModel):
    """本地 shell 宿主配置（docs 03 §3.3；v0.0.5 扩展）。

    设计要点（spec v0.0.5 §3.9）：
    - `env`/`policy` 保留原语义（首期只做本地直连 + confirm 档，`01` §6④）；
    - `kind`：解释器选择。`auto` = 按平台探测（Windows `pwsh`→`powershell`；
      其余 `bash`→`sh`）；显式指定但探测不到 → 宿主 error + 可读原因（不静默降级）；
    - `max_shells`：全局池上限（「一个 Agent 最多唤醒 5 个 shell」的落实）；
    - `idle_timeout_s`：空闲回收阈值（懒检查，零定时器）；
    - `timeout_ms`：单条命令默认超时（docs 09 §6 工具执行超时 30s）；
    - `cwd`：初始工作目录；`None` = 用户主目录；
    - `allow_restricted`：高危命令档。**默认 false**（09 §2 restricted「默认关闭」）→
      命中清单即策略拒绝；置 true 须改配置文件显式启用，运行期不可提权；
    - `tool_permissions`：逐工具权限覆盖（键=工具名，与 MCP 的 `tool_permissions` 同构）；
      有效权限 = 本覆盖 ⊕ ToolSpec 缺省档（09 §2）。
    """

    env: Literal["local"] = "local"
    policy: Literal["confirm"] = "confirm"
    kind: Literal["auto", "pwsh", "powershell", "bash", "sh"] = "auto"
    max_shells: int = 5
    idle_timeout_s: int = 600
    timeout_ms: int = 30000
    cwd: str | None = None
    allow_restricted: bool = False
    tool_permissions: dict[str, Literal["safe", "confirm", "restricted"]] = Field(
        default_factory=dict
    )


class McpServerConfig(BaseModel):
    """单个 MCP server 的落盘配置（v0.0.3）。

    - `transport`：stdio（子进程）/ sse（GET 流 + POST）/ http（Streamable HTTP 单端点）。
    - `command`/`args`/`env`：stdio 专用；env 值为明文（非密钥）。
    - `url`/`headers_ref`：sse/http 专用；`headers_ref` = header 名 → `vault://<id>`（密钥值不入盘）。
    - `tool_permissions`：逐工具权限覆盖（键 = sanitize 后工具名）；缺省 = confirm。
    """

    id: str
    name: str
    enabled: bool = False
    transport: Literal["stdio", "sse", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers_ref: dict[str, str] | None = None
    tool_permissions: dict[str, Literal["safe", "confirm", "restricted"]] | None = None
    timeout_ms: int = 10000


class McpConfig(BaseModel):
    servers: list[McpServerConfig] = Field(default_factory=list)


class ModulesConfig(BaseModel):
    schema_version: Literal[1] = 1
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    dpim: DpimConfig = Field(default_factory=DpimConfig)
    btcm: BtcmConfig = Field(default_factory=BtcmConfig)
    shell: ShellConfig = Field(default_factory=ShellConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)


# ---------------------------------------------------------------------------
# plugins.json
# ---------------------------------------------------------------------------
class BuiltinToolConfig(BaseModel):
    enabled: bool = True
    permission: Literal["safe", "confirm", "restricted"] = "confirm"


class SkillsConfig(BaseModel):
    """Skills 注册数据面（docs 03 §3.4 / 07 §5）。

    - `installed`：已安装技能 id（启动时与 skills/ 目录扫描调和，目录为准）。
    - `enabled`：期望态开关（唯一来源）。
    - `permissions`：逐技能权限档覆盖（v0.0.4；键=技能 id；缺省=SKILL.md frontmatter，
      缺省缺省档 safe）。降权随时可配；提权=落盘显式 + audit（docs 09 §2）。
    """

    installed: list[str] = Field(default_factory=list)
    enabled: list[str] = Field(default_factory=list)
    permissions: dict[str, Literal["safe", "confirm", "restricted"]] = Field(
        default_factory=dict
    )


class PluginsConfig(BaseModel):
    schema_version: Literal[1] = 1
    builtin: dict[str, BuiltinToolConfig] = Field(default_factory=dict)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)


# ---------------------------------------------------------------------------
# 文件名与默认值
# ---------------------------------------------------------------------------
CONFIG_FILES = {
    "models": ("models.json", ModelsConfig),
    "settings": ("settings.json", SettingsConfig),
    "memory": ("memory.json", MemoryConfig),  # v0.0.1：会话记忆出厂默认
    "modules": ("modules.json", ModulesConfig),
    "plugins": ("plugins.json", PluginsConfig),
}
