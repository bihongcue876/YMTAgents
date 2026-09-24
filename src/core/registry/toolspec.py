"""工具描述（spec §2.1 / docs 07 §2.1）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.enums import Permission


class ToolSpec(BaseModel):
    name: str  # 点分命名空间，如 shell.exec / dpim.query
    title: str = ""  # 显示名（UI 用）
    description: str = ""  # 给模型的一句话（环境陈述用）
    #: v0.0.11：工具**使用指引**（spec-2026-09-24-tool-prompts §2.1）。
    #: 与 description 分工：description 说「是什么」，prompt_block 说「何时用 / 要点 / 边界与禁忌」。
    #: 缺省空串；注册期静态校验 <=600 字，超限 fail-closed 拒绝注册。默认只给内置工具填。
    prompt_block: str = ""
    permission: Permission = Permission.CONFIRM  # safe | confirm | restricted
    input_schema: dict = Field(default_factory=dict)  # JSON Schema
    timeout_ms: int = 30000
    availability: Any = None  # 动态可用性回调（宿主 ready 才可用）
    #: 调用**前**策略检查（v0.0.5；v0.0.11 增 allow）：`Callable[[args], (kind, reason) | None]`，
    #: `kind ∈ {"allow", "deny", "warn"}`。`allow` = **策略显式放行**（跳过关卡，
    #: 只降低打扰、从不提权，D-23 α 方案）；`deny` = 策略直接拒绝（走 gate.result(decider=policy)，
    #: **不打扰用户**）；`warn` = 仍然走确认关卡，但卡片按高危档展示。
    #: 用途：让「按参数判定的高危档」（如 shell 的危险命令，docs 09 §2 restricted）
    #: 在权限判定阶段收口，而不是等用户同意后再由后端拒绝。
    precheck: Any = None
    #: v0.0.11（对照模式）：关卡**预览**构建器（Callable[[args], dict] | None）。
    #: 返回内容只随 gate.request 推给 UI（不落 tool.call、不进提示词）；异常按空预览处理。
    preview: Any = None
