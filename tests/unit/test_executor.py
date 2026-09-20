"""core.registry.executor 单元测试（v0.0.3 rev43）。

覆盖七步管线、三档权限、关卡裁决与超时/拒绝、invalid_args、unavailable、审计与落盘。
"""

from __future__ import annotations

from shared.enums import Permission

from core.registry.executor import ToolContext, ToolExecutor
from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec


class _Store:
    def __init__(self) -> None:
        self.events: list = []

    def append_event(self, session_id, event) -> None:
        self.events.append(event)


def _registry() -> Registry:
    reg = Registry()
    reg.register(ToolSpec(name="t.safe", permission=Permission.SAFE), lambda a, c: ToolResult(ok=True, output="ok-safe"))
    reg.register(ToolSpec(name="t.confirm", permission=Permission.CONFIRM), lambda a, c: ToolResult(ok=True, output="ok-confirm"))
    reg.register(ToolSpec(name="t.restricted", permission=Permission.RESTRICTED), lambda a, c: ToolResult(ok=True, output="nope"))
    return reg


def test_safe_tool_auto_and_persist():
    store = _Store()
    emitted: list = []
    ex = ToolExecutor(_registry(), store=store, emit=emitted.append)
    result = ex.execute("c1", "t.safe", {}, ToolContext("s1", 0))
    assert result.ok and result.output == "ok-safe"
    assert [e.type for e in emitted] == ["tool.call", "tool.result"]
    assert [e.type for e in store.events] == ["tool.call", "tool.result"]


def test_confirm_allowed_via_gate():
    emitted: list = []
    seen: list = []
    ex = ToolExecutor(_registry(), emit=emitted.append, gate=lambda *a: (seen.append(a) or True))
    result = ex.execute("c2", "t.confirm", {"x": 1}, ToolContext("s1", 0))
    assert result.ok
    assert seen and seen[0][0] == "c2"
    types = [e.type for e in emitted]
    assert types[0] == "tool.call" and "gate.request" in types and types[-1] == "tool.result"
    assert any(e.type == "gate.result" and e.decision == "allow" for e in emitted)


def test_confirm_denied():
    emitted: list = []
    ex = ToolExecutor(_registry(), emit=emitted.append, gate=lambda *a: False)
    result = ex.execute("c3", "t.confirm", {}, ToolContext("s1", 0))
    assert result.ok is False
    assert result.error["code"] == "tool_denied"
    assert any(e.type == "gate.result" and e.decision == "deny" for e in emitted)


def test_confirm_no_gate_fails_closed():
    ex = ToolExecutor(_registry(), emit=lambda e: None)
    result = ex.execute("c4", "t.confirm", {}, None)
    assert result.ok is False and result.error["code"] == "tool_denied"


def test_restricted_denied_by_policy():
    emitted: list = []
    ex = ToolExecutor(_registry(), emit=emitted.append)
    result = ex.execute("c5", "t.restricted", {}, None)
    assert result.ok is False and result.error["code"] == "tool_denied"
    assert any(e.type == "gate.result" and e.decider == "policy" for e in emitted)


def test_invalid_args_not_executed():
    store = _Store()
    ex = ToolExecutor(_registry(), store=store, emit=lambda e: None)
    result = ex.execute_raw("c6", "t.safe", "{bad json", ToolContext("s1", 0))
    assert result.ok is False and result.error["code"] == "tool_invalid_args"
    assert [e.type for e in store.events] == ["tool.call", "tool.result"]


def test_unknown_tool_unavailable():
    ex = ToolExecutor(_registry(), emit=lambda e: None)
    result = ex.execute("c7", "missing.tool", {}, None)
    assert result.ok is False and result.error["code"] == "tool_unavailable"


def test_availability_hidden_tool_unavailable():
    reg = Registry()
    reg.register(
        ToolSpec(name="t.hidden", permission=Permission.SAFE, availability=lambda: False),
        lambda a, c: ToolResult(ok=True, output="should-not-run"),
    )
    ex = ToolExecutor(reg, emit=lambda e: None)
    assert ex.tool_payloads() == []  # 不可见 => 不下发
    result = ex.execute("c8", "t.hidden", {}, None)
    assert result.ok is False and result.error["code"] == "tool_unavailable"


def test_tools_tokens_positive():
    ex = ToolExecutor(_registry(), emit=lambda e: None)
    assert ex.tools_tokens() > 0


class _OutputStore:
    """带外置能力的会话存储替身（v0.0.3 完善 output_ref）。"""

    def __init__(self) -> None:
        self.events: list = []
        self.written: dict[str, str] = {}

    def append_event(self, session_id, event) -> None:
        self.events.append(event)

    def write_output(self, session_id: str, call_id: str, text: str) -> str:
        ref = f"outputs/{call_id}.txt"
        self.written[ref] = text
        return ref


def test_large_output_is_externalised():
    """超过内联上限：全文外置，事件流只留预览 + output_ref。"""
    reg = Registry()
    big = "x" * 20000
    reg.register(ToolSpec(name="t.big", permission=Permission.SAFE), lambda a, c: ToolResult(ok=True, output=big))
    store = _OutputStore()
    emitted: list = []
    ex = ToolExecutor(reg, store=store, emit=emitted.append)
    result = ex.execute("c9", "t.big", {}, ToolContext("s1", 0))
    assert result.ok and result.output_ref == "outputs/c9.txt"
    assert store.written["outputs/c9.txt"] == big
    assert len(result.output) < len(big)
    assert "outputs/c9.txt" in result.output
    event = next(e for e in emitted if e.type == "tool.result")
    assert event.output_ref == "outputs/c9.txt"