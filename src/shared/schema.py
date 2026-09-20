"""配置文件 schema（docs 03 §3）。

对应 `ymtdata/config/` 下的四个 JSON 文件，均带 `schema_version: 1`。
与 spec 的「下发视图」类型（ProviderSpec 等）不同：本模块是**落盘形态**，
供应商携带 `key_ref`（引用本地加密库），永不携带明文密钥。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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


class SettingsConfig(BaseModel):
    schema_version: Literal[1] = 1
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    ui: UISettings = Field(default_factory=UISettings)


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
class DpimConfig(BaseModel):
    enabled: bool = False
    joint: dict = Field(default_factory=lambda: {"default_libs": [], "top_k": 8})


class BtcmConfig(BaseModel):
    enabled: bool = False
    trigger: Literal["manual", "auto", "off"] = "off"
    slot: Literal["main", "thinking", "fast", "embedding"] = "thinking"


class ShellConfig(BaseModel):
    env: Literal["local"] = "local"
    policy: Literal["confirm"] = "confirm"


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
    enabled: bool = False
    servers: list[McpServerConfig] = Field(default_factory=list)


class ModulesConfig(BaseModel):
    schema_version: Literal[1] = 1
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
    installed: list[str] = Field(default_factory=list)
    enabled: list[str] = Field(default_factory=list)


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
