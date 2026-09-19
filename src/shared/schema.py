"""配置文件 schema（docs 03 §3）。

对应 `ymtdata/config/` 下的四个 JSON 文件，均带 `schema_version: 1`。
与 spec 的「下发视图」类型（ProviderSpec 等）不同：本模块是**落盘形态**，
供应商携带 `key_ref`（引用系统凭据管理器），永不携带明文密钥。
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


class ProviderConfig(BaseModel):
    id: str  # prv_<uuid7>
    name: str
    base_url: str
    key_ref: str | None = None  # keyring://ymt/<prv_id>；None = 未设置密钥
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
# summary.json（rev26：历史摘要化的**出厂默认**，阈值为用户可改的数值）
# ---------------------------------------------------------------------------
class SummaryConfig(BaseModel):
    """压缩（历史摘要化）的全局出厂默认；按会话只覆盖 `threshold`。

    - `threshold`：占用百分比，**由用户指定**；占用达到该值才**允许**自动压缩，
      低于它绝不压（用户裁决 2026-09-19）。
    - `auto`：自动压缩开关，默认关（压缩不轻易做）。
    - `keep_ratio`：尾部保留 = 生效窗口 // keep_ratio（默认 1/8），保证近期对话不进摘要。
    - `model_slot`：摘要调用所用槽位；None = 用会话当前模型。
    """

    schema_version: Literal[1] = 1
    threshold: int = 90
    auto: bool = False
    keep_ratio: int = 8
    model_slot: Literal["main", "thinking", "fast", "embedding"] | None = None


# ---------------------------------------------------------------------------
# sessions/<id>/summary.json（rev26：摘要**状态**；正文在同目录 summary.md）
# ---------------------------------------------------------------------------
class SessionSummary(BaseModel):
    """一次历史摘要化的落盘状态（append-only 事实源 `events.jsonl` 不动）。"""

    schema_version: Literal[1] = 1
    revision: int = 0
    covered_seq: int = -1  # 已被摘要覆盖到的最后一个事件 seq；-1 = 尚未压缩
    model: str | None = None  # 生成该摘要的模型
    tokens_est: int = 0  # 摘要正文的估算 token（装配时占用的量）
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


class McpConfig(BaseModel):
    enabled: bool = False
    servers: list[dict] = Field(default_factory=list)


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
    "summary": ("summary.json", SummaryConfig),  # rev26：压缩出厂默认
    "modules": ("modules.json", ModulesConfig),
    "plugins": ("plugins.json", PluginsConfig),
}
