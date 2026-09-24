"""切片 C：工具使用指引段与 prompt_block（v0.0.11）。"""

from __future__ import annotations

from core.agent import loop as loop_module
from core.agent.context import ConfigSnapshot, _env_statement
from core.agent.loop import AgentLoop
from core.registry.executor import ToolExecutor
from core.registry.registry import Registry
from core.registry.toolspec import ToolSpec


class _FakeExecutor:
    def __init__(self, blocks):
        self._blocks = blocks

    def tool_prompt_blocks(self):
        return list(self._blocks)


class _Stub:
    def __init__(self, blocks):
        self.executor = _FakeExecutor(blocks)


def test_env_statement_omits_guide_section_when_empty():
    text = _env_statement(ConfigSnapshot(tool_lines=["file.read — 读文件"]))
    assert "工具使用指引" not in text
    assert "当前可用工具" in text


def test_env_statement_renders_guide_and_boundaries():
    config = ConfigSnapshot(
        tool_lines=["file.read — 读文件"],
        tool_guide_lines=["file.read：何时用 / 怎么用"],
    )
    text = _env_statement(config)
    assert "工具使用指引" in text and "file.read：何时用 / 怎么用" in text
    assert "未列出的工具当前不可用" in text
    assert "缺少所需工具时，如实说明缺什么" in text
    assert text.index("工具使用指引") < text.index("已挂载文件")


def test_guide_lines_keep_in_budget():
    stub = _Stub([("file.read", "读文件并分页"), ("mcp.s1.tool", "远程工具")])
    lines, dropped = AgentLoop._tool_guide_lines(stub)
    assert dropped == 0
    assert lines[0].startswith("file.read：")


def test_guide_lines_drop_over_budget(monkeypatch):
    stub = _Stub([("file.read", "x" * 20000), ("mcp.s1.tool", "y")])
    monkeypatch.setattr(loop_module, "_TOOL_GUIDE_TOKENS", 0)
    lines, dropped = AgentLoop._tool_guide_lines(stub)
    assert lines == [] and dropped == 2


def test_executor_orders_blocks_and_skips_empty():
    registry = Registry()
    entries = [("mcp.x.t", "m"), ("skill.demo", "s"), ("file.read", "f"), ("shell.exec", "")]
    for name, block in entries:
        registry.register(ToolSpec(name=name, prompt_block=block), lambda args, ctx: None)
    blocks = ToolExecutor(registry).tool_prompt_blocks()
    assert [name for name, _block in blocks] == ["file.read", "skill.demo", "mcp.x.t"]
