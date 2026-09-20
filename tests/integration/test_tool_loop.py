"""工具回路集成测试（v0.0.3 rev42/rev43）：模拟 tool_calls → 执行 → 回注 → 收束。

以 MockGateway 脚本化 tool_calls，端到端走 `AgentLoop.run_turn`，
校验 tool.call/tool.result 落盘、assistant.tool_calls + tool 消息回注、历史可重建。
"""

from __future__ import annotations

from shared.enums import Permission
from shared.envelope import SendMessage
from core.agent.context import _history_messages
from core.agent.loop import AgentLoop
from core.agent.session import SessionStore
from core.registry.executor import ToolExecutor
from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec
from core.store.config_store import ConfigStore
from tests.mocks.gateway import MockGateway


def _make(tmp_path, rounds, chunks=("最终", "答复")):
    store = SessionStore(tmp_path)
    ConfigStore(tmp_path).ensure_defaults()
    registry = Registry()
    registry.register(
        ToolSpec(name="demo.echo", permission=Permission.SAFE),
        lambda args, ctx: ToolResult(ok=True, output="echo:" + str(args.get("text", ""))),
    )
    emitted: list = []
    executor = ToolExecutor(registry, store=store, emit=emitted.append)
    gateway = MockGateway(chunks=chunks, tool_call_rounds=rounds)
    loop = AgentLoop(store, gateway, emitted.append, tmp_path, executor=executor)
    return store, gateway, loop, emitted


def test_react_tool_loop_end_to_end(tmp_path):
    rounds = [[{"id": "call_1", "name": "demo.echo", "arguments": '{"text": "hi"}'}]]
    store, gateway, loop, emitted = _make(tmp_path, rounds)
    meta = store.create(None, None)

    loop.run_turn(meta.id, SendMessage(text="帮我调用工具"))

    types = [e.type for e in emitted]
    assert "tool.call" in types and "tool.result" in types
    assert types[-1] == "turn.status" and emitted[-1].state == "done"
    final = [e for e in emitted if e.type == "msg.assistant.final"][-1]
    assert final.content == "最终答复"

    # 第一次调用带工具定义；第二次调用回注了 assistant.tool_calls + tool 消息
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["tools"]  # 工具定义已下发
    second = gateway.calls[1]["messages"]
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second)
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_1"
    assert tool_msgs[0]["content"] == "echo:hi"

    # 落盘：tool.call / tool.result 持久化（append-only）
    persisted = [e.get("type") for e in store.replay(meta.id)]
    assert "tool.call" in persisted and "tool.result" in persisted


def test_tool_history_reconstruction(tmp_path):
    rounds = [[{"id": "call_1", "name": "demo.echo", "arguments": '{"text": "hi"}'}]]
    store, _gateway, loop, _emitted = _make(tmp_path, rounds)
    meta = store.create(None, None)
    loop.run_turn(meta.id, SendMessage(text="q"))

    messages = _history_messages(store.replay(meta.id))
    roles = [m["role"] for m in messages]
    # user → assistant(tool_calls) → tool → assistant(final)
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["function"]["name"] == "demo.echo"
    assert messages[2]["tool_call_id"] == "call_1"
    assert messages[3]["content"] == "最终答复"


def test_no_executor_behaves_like_first_phase(tmp_path):
    store = SessionStore(tmp_path)
    ConfigStore(tmp_path).ensure_defaults()
    emitted: list = []
    gateway = MockGateway(chunks=("你好",))
    loop = AgentLoop(store, gateway, emitted.append, tmp_path)
    meta = store.create(None, None)
    loop.run_turn(meta.id, SendMessage(text="hi"))
    assert [e for e in emitted if e.type == "msg.assistant.final"][-1].content == "你好"
    assert gateway.calls[0]["tools"] is None  # 无执行器 => 不下发工具