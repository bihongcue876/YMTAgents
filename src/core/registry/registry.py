"""统一工具注册表（spec §2.1 / docs 07 §2）。

首期空载契约：`snapshot()` 返回空列表；注册协议与执行接口就位。
- 注册表永不 import 具体后端；模块 import 注册表完成注册（04 §1）。
- 注册冲突报错，禁止静默覆盖（docs 09 §4 B5）。
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from core.registry.toolspec import ToolSpec


class ToolResult(BaseModel):
    ok: bool
    output: str | None = None
    output_ref: str | None = None
    error: dict | None = None
    usage: dict | None = None
    duration_ms: int = 0


Handler = Callable[[dict, Any], ToolResult]


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Handler]] = {}

    def register(self, spec: ToolSpec, handler: Handler) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具注册冲突：{spec.name}（禁止静默覆盖）")
        self._tools[spec.name] = (spec, handler)

    def snapshot(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

    def list_tools(self, predicate: Callable[[ToolSpec], bool] | None = None) -> list[ToolSpec]:
        specs = self.snapshot()
        return specs if predicate is None else [s for s in specs if predicate(s)]

    def execute(self, name: str, args: dict, ctx: Any = None) -> ToolResult:
        entry = self._tools.get(name)
        if entry is None:
            return ToolResult(ok=False, error={"code": "unavailable", "message": "工具不存在或未注册"})
        _spec, handler = entry
        return handler(args, ctx)
