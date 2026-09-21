"""执行器对 skill.* 的豁免测试（v0.0.4 / spec §2.3）：正文是一等输入，不外置。"""

from __future__ import annotations

from core.registry.executor import ToolExecutor, ToolContext
from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec
from shared.enums import Permission


class _Store:
    def __init__(self):
        self.written = []

    def write_output(self, session_id, call_id, output):
        self.written.append(call_id)
        return f"outputs/{call_id}.txt"

    def append_event(self, session_id, event):
        pass


def _boot():
    registry = Registry()
    store = _Store()
    executor = ToolExecutor(registry, store=store)
    return registry, executor, store


def test_skill_output_not_externalized():
    registry, executor, store = _boot()
    big = "指令" * 6000  # > 8000 字符
    registry.register(
        ToolSpec(name="skill.skl_a", title="t", description="d", permission=Permission.SAFE),
        lambda _a, _c: ToolResult(ok=True, output=big),
    )
    result = executor.execute("c1", "skill.skl_a", {}, ToolContext(session_id="s1"))
    assert result.ok is True
    assert result.output == big  # 全文回注模型（渐进披露依赖全文）
    assert result.output_ref is None
    assert store.written == []


def test_other_tool_output_still_externalized():
    registry, executor, store = _boot()
    big = "x" * 9000
    registry.register(
        ToolSpec(name="mcp.s.t", title="t", description="d", permission=Permission.SAFE),
        lambda _a, _c: ToolResult(ok=True, output=big),
    )
    result = executor.execute("c2", "mcp.s.t", {}, ToolContext(session_id="s1"))
    assert result.output_ref is not None
    assert len(result.output) < len(big)
    assert store.written == ["c2"]
