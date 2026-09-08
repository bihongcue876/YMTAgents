"""core.agent 单元测试（上下文 / 会话存储 / 回合循环）。"""

from __future__ import annotations

from shared.envelope import AssistantFinal, ModelSpec, ProviderSpec, SendMessage, Usage
from core.agent.context import ConfigSnapshot, ContextAssembler
from core.agent.loop import AgentLoop
from core.agent.session import SessionStore
from core.store.config_store import ConfigStore


class FakeGateway:
    def __init__(self, chunks=("你", "好"), slots=None, cancel_after=None):
        self._chunks = list(chunks)
        self._slots = slots if slots is not None else {"main": "m1"}
        self._cancel_after = cancel_after

    def get_slots(self):
        return dict(self._slots)

    def list_providers(self):
        return [
            ProviderSpec(
                id="prv",
                name="P",
                base_url="https://api.test.com",
                models=[ModelSpec(id="m1", ctx_window=1000)],
            )
        ]

    def stream_chat(self, session_id, turn_seq, model_id, messages, cancel_token, on_delta):
        for i, chunk in enumerate(self._chunks):
            on_delta(chunk)
            if self._cancel_after is not None and i == self._cancel_after and cancel_token:
                cancel_token.cancel()
        return Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def make_store(tmp_path):
    store = SessionStore(tmp_path)
    ConfigStore(tmp_path).ensure_defaults()
    return store


def test_session_lifecycle(tmp_path):
    store = make_store(tmp_path)
    meta = store.create("测试", None)
    assert store.get_meta(meta.id).title == "测试"
    assert len(store.list()) == 1

    store.rename(meta.id, "改名")
    assert store.get_meta(meta.id).title == "改名"

    store.set_model(meta.id, "m1")
    assert store.get_meta(meta.id).main_model == "m1"

    store.archive(meta.id)
    assert store.list() == []
    assert len(store.list(include_archived=True)) == 1
    store.unarchive(meta.id)
    assert len(store.list()) == 1

    store.delete(meta.id)
    assert store.list(include_archived=True) == []
    assert (tmp_path / "sessions" / meta.id).exists()  # 目录保留


def test_replay_reconstructs(tmp_path):
    store = make_store(tmp_path)
    meta = store.create(None, None)
    store.append_event(meta.id, SendMessage(text="你好"))
    store.append_event(meta.id, AssistantFinal(content="应答", turn_seq=0))
    events = store.replay(meta.id)
    types = [e["type"] for e in events]
    assert types == ["session.start", "msg.user", "msg.assistant.final"]


def test_context_deterministic(tmp_path):
    store = make_store(tmp_path)
    meta = store.create(None, None)
    store.append_event(meta.id, SendMessage(text="第一问"))
    snap = store.resume(meta.id)
    config = ConfigSnapshot(system_prompt="SYS", memory="MEM", window=1000, reserve=100)
    assembler = ContextAssembler()
    a_msgs, a_usage = assembler.build(snap, config, 900)
    b_msgs, b_usage = assembler.build(snap, config, 900)
    assert a_msgs == b_msgs
    assert a_usage.segments == b_usage.segments
    assert a_usage.total == b_usage.total
    assert a_msgs[0]["role"] == "system"
    assert a_usage.segments["reserve"] == 100


def test_context_drops_oldest(tmp_path):
    store = make_store(tmp_path)
    meta = store.create(None, None)
    for i in range(5):
        store.append_event(meta.id, SendMessage(text=f"问题{i}"))
    snap = store.resume(meta.id)
    config = ConfigSnapshot(system_prompt="", memory="", history_turns=2, reserve=0, window=0)
    messages, usage = ContextAssembler().build(snap, config, 10**9)
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert [m["content"] for m in user_msgs] == ["问题3", "问题4"]


def test_loop_turn_and_interrupt(tmp_path):
    store = make_store(tmp_path)
    meta = store.create(None, None)
    emitted: list = []
    loop = AgentLoop(store, FakeGateway(), emitted.append, tmp_path)

    loop.run_turn(meta.id, SendMessage(text="hi"))
    types = [e.type for e in emitted]
    assert "msg.assistant.delta" in types
    assert types[-1] == "turn.status"
    assert emitted[-1].state == "done"
    finals = [e for e in emitted if e.type == "msg.assistant.final"]
    assert finals and finals[0].content == "你好"

    # 中断
    emitted.clear()
    loop2 = AgentLoop(store, FakeGateway(cancel_after=0), emitted.append, tmp_path)
    loop2.run_turn(meta.id, SendMessage(text="again"))
    final = [e for e in emitted if e.type == "msg.assistant.final"][-1]
    assert final.interrupted is True
    assert any(e["type"] == "interrupt" for e in store.replay(meta.id))
