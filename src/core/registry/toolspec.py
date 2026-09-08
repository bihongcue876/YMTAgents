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
