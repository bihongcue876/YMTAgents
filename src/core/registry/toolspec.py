"""工具描述（spec §2.1 / docs 07 §2.1）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.enums import Permission


class ToolSpec(BaseModel):
    name: str  # 点分命名空间，如 shell.exec / dpim.query
    title: str = ""  # 显示名（UI 用）
    description: str = ""  # 给模型的一句话（环境陈述用）
    permission: Permission = Permission.CONFIRM  # safe | confirm | restricted
    input_schema: dict = Field(default_factory=dict)  # JSON Schema
    timeout_ms: int = 30000
    availability: Any = None  # 动态可用性回调（宿主 ready 才可用）
    #: 调用**前**策略检查（v0.0.5）：`Callable[[args], (kind, reason) | None]`，
    #: `kind ∈ {"deny", "warn"}`。`deny` = 策略直接拒绝（走 gate.result(decider=policy)，
    #: **不打扰用户**）；`warn` = 仍然走确认关卡，但卡片按高危档展示。
    #: 用途：让「按参数判定的高危档」（如 shell 的危险命令，docs 09 §2 restricted）
    #: 在权限判定阶段收口，而不是等用户同意后再由后端拒绝。
    precheck: Any = None
