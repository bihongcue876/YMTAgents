"""配置文件 schema（docs 03 §3）。

对应 `ymtdata/config/` 下的四个 JSON 文件，均带 `schema_version: 1`。
与 spec 的「下发视图」类型（ProviderSpec 等）不同：本模块是**落盘形态**，
供应商携带 `key_ref`（引用系统凭据管理器），永不携带明文密钥。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# models.json
# ---------------------------------------------------------------------------
class ModelConfig(BaseModel):
    id: str
    ctx_window: int = 0  # 0 = 未知
    tags: list[str] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    id: str  # prv_<uuid7>
    name: str
    base_url: str
    key_ref: str | None = None  # keyring://ymt/<prv_id>；None = 未设置密钥
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
class ContextSettings(BaseModel):
    history_turns: int = 20  # 历史保留轮数 N
    reserve: int = 4096  # 输出预留
    file_truncate: int = 8192  # 挂载文件截断上限（token 近似）


class NetworkSettings(BaseModel):
    whitelist: list[str] = Field(default_factory=list)  # 域名精确或 *.后缀


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"


class UISettings(BaseModel):
    """UI 选项。theme 取值与 `gui.theme.PALETTES` 一致（docs 05 §1）。"""

    theme: Literal["light", "dark"] = "light"


class SettingsConfig(BaseModel):
    schema_version: Literal[1] = 1
    context: ContextSettings = Field(default_factory=ContextSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    ui: UISettings = Field(default_factory=UISettings)


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
    "modules": ("modules.json", ModulesConfig),
    "plugins": ("plugins.json", PluginsConfig),
}
