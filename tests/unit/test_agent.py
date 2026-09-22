"""core.agent 单元测试（上下文 / 会话存储 / 回合循环）。"""

from __future__ import annotations

import pytest

from shared.envelope import AssistantFinal, ModelSpec, ProviderSpec, SendMessage, Usage
from core.agent.context import (
    DEFAULT_SYSTEM_PROMPT,
    TRUNCATION_MARK,
    ConfigSnapshot,
    ContextAssembler,
    _truncate_to_tokens,
    estimate_tokens,
)
from core.agent.loop import AgentLoop
from core.agent.session import SessionStore
from core.store.config_store import ConfigStore


def test_default_prompt_is_objective():
    """回归锚点（rev16）：底层提示词不得自述身份与能力 —— 产品叙事归 persona 轮的 YMT 角色。

    「你是……超级 Agent」这类第一人称介绍是应用替自己设计的定位，
    客观的做法是只约定行为（语言、诚实），身份由可编辑的角色承载。
    """
    assert "超级" not in DEFAULT_SYSTEM_PROMPT
    assert "你是" not in DEFAULT_SYSTEM_PROMPT  # 不做第一人称身份叙述
    assert ConfigSnapshot().system_prompt == DEFAULT_SYSTEM_PROMPT


class FakeGateway:
    def __init__(
        self,
        chunks=("你", "好"),
        slots=None,
        cancel_after=None,
        ctx_window=1000,
        reasoning_chunks=None,
        reasoning_pending=False,
    ):
        self._chunks = list(chunks)
        self._reasoning_chunks = list(reasoning_chunks or [])
        self._slots = slots if slots is not None else {"main": "m1"}
        self._cancel_after = cancel_after
        self._ctx_window = ctx_window
        self._reasoning_pending = reasoning_pending
        self.probed: list[str] = []
        self.last_params = None
        self.last_messages: list[dict] | None = None

    def get_slots(self):
        return dict(self._slots)

    def list_providers(self):
        return [
            ProviderSpec(
                id="prv",
                name="P",
                base_url="https://api.test.com",
                models=[ModelSpec(id="m1", ctx_window=self._ctx_window)],
            )
        ]

    def reasoning_pending(self, model_id):
        return self._reasoning_pending

    def probe_reasoning(self, model_id):
        self.probed.append(model_id)
        return "yes"

    def stream_chat(
        self,
        session_id,
        turn_seq,
        model_id,
        messages,
        cancel_token,
        on_delta,
        on_reasoning=None,
        params=None,
        tools=None,
        on_tool_calls=None,
    ):
        self.last_params = params
        self.last_messages = [dict(m) for m in messages]
        for chunk in self._reasoning_chunks:
            if on_reasoning is not None:
                on_reasoning(chunk)
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


def test_context_eviction_is_token_budget_not_turn_count(tmp_path):
    """回归锚点（rev24）：历史只受 **token 预算**约束，不设「保留最近 N 轮」。

    用户裁决：这是对话应用，对话不能被轻易丢弃；只有真正超出窗口时才从最旧处淘汰，
    且当前提问必须留下。
    """
    store = make_store(tmp_path)
    meta = store.create(None, None)
    for i in range(5):
        store.append_event(meta.id, SendMessage(text=f"问题{i}"))
    snap = store.resume(meta.id)
    config = ConfigSnapshot(system_prompt="", memory="", reserve=0, window=0)

    messages, _usage = ContextAssembler().build(snap, config, 10**9)
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        f"问题{i}" for i in range(5)
    ], "预算充足时不得按轮数丢弃历史"

    messages2, _usage2 = ContextAssembler().build(snap, config, 3)
    users2 = [m["content"] for m in messages2 if m["role"] == "user"]
    assert users2[-1] == "问题4", "当前提问永不被淘汰"
    assert len(users2) < 5, "预算极紧时从最旧处淘汰"


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


# -- 上下文淘汰与截断（spec rev8 §2） -----------------------------------------


def _fill_turns(store, meta, n: int) -> None:
    for i in range(n):
        store.append_event(meta.id, SendMessage(text=f"问题{i}"))
        store.append_event(meta.id, AssistantFinal(content=f"回答{i}", turn_seq=i))


def test_context_never_drops_the_current_question(tmp_path):
    """回归锚点：预算再紧，**当前提问也必须留下**。

    实证：旧实现 `while history and total() > budget: history.pop(0)` 会一路 pop 到空 ——
    非 system 消息剩 0 条，模型收到「只有 system、没有提问」，用户侧却毫无提示。
    rev20 起：文件不可被淘汰也不得撑爆预算 → 超出「输入预算 − system − env」时
    整块按余量截断（带标记，用户可见），当前提问照旧保留。
    """
    store = make_store(tmp_path)
    meta = store.create(None, None)
    _fill_turns(store, meta, 3)
    config = ConfigSnapshot(
        system_prompt="SYS",
        memory="",
        files=[("big.txt", "啊" * 20000)],
        file_truncate=0,  # 0 = 每文件不截断；由总额护栏兜底
        reserve=0,
    )
    messages, _usage = ContextAssembler().build(store.resume(meta.id), config, 100)
    assert [m["content"] for m in messages if m["role"] == "user"] == ["问题2"]
    assert messages[0]["role"] == "system"
    assert "内容已截断]" in messages[0]["content"], "文件超预算必须截断并带可见标记"


def test_context_file_truncate_is_per_file(tmp_path):
    """回归锚点（rev20）：`file_truncate` 语义 = **每文件**上限。

    旧实现是全部文件共享一个总额（8K），挂两个文件各分一半 ——
    对超级 Agent 的文件工作流完全不够用。新语义：每个文件各自截断到上限。
    """
    store = make_store(tmp_path)
    meta = store.create(None, None)
    store.append_event(meta.id, SendMessage(text="问"))
    config = ConfigSnapshot(
        system_prompt="",
        memory="",
        files=[("a.txt", "a" * 30000), ("b.txt", "b" * 30000)],  # 各 ≈7500 tokens
        file_truncate=8000,
        reserve=0,
    )
    messages, usage = ContextAssembler().build(store.resume(meta.id), config, 10**9)
    content = messages[0]["content"]
    assert content.count("a") > 6000 and content.count("b") > 6000, "两个文件都必须完整保留"
    assert usage.segments["files"] > 12000, "总额不再被单个 8K 上限卡死"


def test_context_files_capped_by_input_budget(tmp_path):
    """回归锚点（rev20）：文件不可淘汰，故总额对「输入预算 − system − env」护栏。"""
    store = make_store(tmp_path)
    meta = store.create(None, None)
    store.append_event(meta.id, SendMessage(text="问"))
    config = ConfigSnapshot(
        system_prompt="",
        memory="",
        files=[("f.txt", "啊" * 30000)],  # ≈30000 tokens，远超预算
        file_truncate=0,  # 每文件不限，逼出总额护栏
        reserve=0,
        window=1000,
    )
    messages, usage = ContextAssembler().build(store.resume(meta.id), config, 1000)
    assert estimate_tokens(messages[0]["content"]) <= 1000 + len("内容已截断]")
    assert "内容已截断]" in messages[0]["content"]
    assert usage.total <= 1000 + usage.segments["reserve"] + len("内容已截断]")


def test_context_keeps_question_even_with_negative_budget(tmp_path):
    """window < reserve 时预算为负（配置不自洽），同样不得丢掉当前提问。"""
    store = make_store(tmp_path)
    meta = store.create(None, None)
    _fill_turns(store, meta, 2)
    config = ConfigSnapshot(system_prompt="", memory="", reserve=4096)
    messages, _usage = ContextAssembler().build(store.resume(meta.id), config, -1000)
    assert [m["content"] for m in messages if m["role"] == "user"] == ["问题1"]


def test_context_eviction_does_not_double_count_reserve(tmp_path):
    """回归锚点：淘汰按**输入侧**用量判，不含 reserve。

    旧实现拿 total（含 reserve）比 budget（= window - reserve），等于把 reserve 扣两次，
    会淘汰远超需要的上下文。此处输入侧用量约 80、reserve=100、budget=150：
    输入侧并未超预算，故三条历史都应保留（旧实现会把它们全部淘汰）。
    """
    store = make_store(tmp_path)
    meta = store.create(None, None)
    _fill_turns(store, meta, 3)
    config = ConfigSnapshot(system_prompt="S", memory="", reserve=100)
    messages, usage = ContextAssembler().build(store.resume(meta.id), config, 150)
    users = [m["content"] for m in messages if m["role"] == "user"]
    assert users == ["问题0", "问题1", "问题2"]
    assert usage.segments["reserve"] == 100
    assert usage.total == usage.segments["history"] + usage.segments["system"] + usage.segments["env"] + usage.segments["files"] + 100


def test_file_truncate_budget_is_tokens_not_characters():
    """回归锚点：`file_truncate` 的口径是 token，比较与截断必须同单位。

    旧实现用 token 判定却按**字符**切：拉丁文本 4000 字符（≈1000 tokens）在 limit=100 下
    只留 100 字符（≈25 tokens），可用额度被浪费掉约 3/4。
    截断结果带「已截断」标记，标记占用 token 已从预算中扣除（长度断言因此放宽一个标记）。
    """
    latin = "a" * 4000
    out = _truncate_to_tokens(latin, 100)
    assert len(out) > 100  # 旧实现恰好 100
    assert len(out) >= 380
    assert estimate_tokens(out) <= 100
    assert out.endswith("内容已截断]")

    cjk = _truncate_to_tokens("啊" * 5000, 100)
    assert len(cjk) <= 100 + len(TRUNCATION_MARK)
    assert estimate_tokens(cjk) <= 100
    assert cjk.endswith("内容已截断]")

    assert _truncate_to_tokens(latin, 0) == latin  # 0 = 不截断（沿用既有约定）
    assert _truncate_to_tokens("short", 100) == "short"  # 未超限不动、也不加标记
    assert _truncate_to_tokens(latin, 3) == TRUNCATION_MARK  # 预算只够放标记


def test_context_applies_file_truncate_end_to_end(tmp_path):
    """挂载文件的截断在组装路径上生效（不是只有辅助函数正确）。"""
    store = make_store(tmp_path)
    meta = store.create(None, None)
    store.append_event(meta.id, SendMessage(text="问"))
    config = ConfigSnapshot(
        system_prompt="", memory="", files=[("f.txt", "a" * 4000)], file_truncate=100, reserve=0
    )
    messages, usage = ContextAssembler().build(store.resume(meta.id), config, 10**9)
    assert messages[0]["content"].count("a") > 100
    assert usage.segments["files"] <= 110  # 含 "[文件：f.txt]" 前缀


def test_loop_reports_context_overflow(tmp_path):
    """回归锚点：输入超出模型窗口时本地收口为 `context_overflow`。

    该码此前**零发射**：请求照发，被上游拒绝后还会被 rev5 的归因口径误报成
    `model_not_found`，把用户指向「换模型」这个错误方向。
    """
    store = make_store(tmp_path)
    meta = store.create(None, None)
    emitted: list = []
    loop = AgentLoop(store, FakeGateway(ctx_window=60), emitted.append, tmp_path)
    loop.run_turn(meta.id, SendMessage(text="很长的问题" * 100))

    errors = [e for e in emitted if e.type == "error"]
    assert errors and errors[0].code == "context_overflow"
    assert "窗口" in errors[0].message
    assert emitted[-1].type == "turn.status" and emitted[-1].state == "failed"
    assert not any(e.type == "turn.status" and e.state == "calling" for e in emitted)


# -- 上下文预算自适应（spec rev20） ---------------------------------------------


def test_effective_reserve_scales_with_window():
    """用户裁决：预算要按 200K/300K/1M 级窗口的尺度来，且配置值是下限。"""
    from core.agent.loop import effective_reserve

    assert effective_reserve(4096, 200_000) == 25_000  # ≈1/8
    assert effective_reserve(4096, 300_000) == 32_768  # 封顶
    assert effective_reserve(4096, 1_000_000) == 32_768  # 封顶
    assert effective_reserve(8192, 128_000) == 16_000  # 配置值更大则取更大者
    assert effective_reserve(4096, 8_000) == 2_000  # 小窗口被 1/4 上限压回，保输入侧
    assert effective_reserve(4096, 0) == 4096  # 窗口未知用配置值
    assert effective_reserve(65536, 100_000) == 25_000  # 超配也会被 1/4 上限压回


def test_effective_file_cap_scales_with_window():
    from core.agent.loop import effective_file_cap

    assert effective_file_cap(8192, 200_000) == 50_000  # ≈1/4
    assert effective_file_cap(8192, 1_000_000) == 65_536  # 封顶
    assert effective_file_cap(8192, 8_000) == 8_192  # 小窗口：配置值即上限（只增不减）
    assert effective_file_cap(8192, 0) == 8192  # 窗口未知用配置值


# -- 会话详情与逐会话策略（spec rev24） -----------------------------------------


def test_session_update_roundtrip_and_data_bytes(tmp_path):
    """逐会话策略：update 覆盖名称/作用/上限/参数，data_bytes 反映落盘体积。"""
    from shared.envelope import SessionParams

    store = make_store(tmp_path)
    meta = store.create("原名", None)
    base = store.data_bytes(meta.id)
    assert base > 0, "session.start 已落盘"

    store.append_event(meta.id, SendMessage(text="你好"))
    assert store.data_bytes(meta.id) > base, "追加事件后体积增长"

    updated = store.update(
        meta.id,
        title="改名",
        note="作用说明",
        max_context=64_000,
        params=SessionParams(temperature=0.5, top_k=40),
    )
    assert updated.title == "改名"
    assert updated.note == "作用说明"
    assert updated.max_context == 64_000
    assert updated.params.temperature == 0.5
    assert updated.params.top_k == 40
    assert updated.params.top_p is None

    reloaded = store.get_meta(meta.id)
    assert reloaded.max_context == 64_000
    assert reloaded.params.temperature == 0.5

    # 空标题表示保持不变
    kept = store.update(
        meta.id, title="", note="", max_context=None, params=SessionParams()
    )
    assert kept.title == "改名"
    assert kept.note is None
    assert kept.max_context is None


def test_param_options_only_sends_enabled(tmp_path):
    from shared.envelope import SessionParams
    from core.gateway.provider import _param_options

    assert _param_options(None) == {}
    assert _param_options(SessionParams()) == {}
    opts = _param_options(SessionParams(temperature=0.2, top_k=20))
    assert opts == {"temperature": 0.2, "top_k": 20}
    assert "top_p" not in opts and "max_tokens" not in opts


def test_loop_passes_session_params_to_gateway(tmp_path):
    from shared.envelope import SessionParams

    store = make_store(tmp_path)
    meta = store.create(None, None)
    store.update(
        meta.id,
        title="",
        note="",
        max_context=100_000,
        params=SessionParams(temperature=0.3),
    )
    gw = FakeGateway()
    loop = AgentLoop(store, gw, lambda e: None, tmp_path)
    loop.run_turn(meta.id, SendMessage(text="hi"))
    assert gw.last_params is not None
    assert gw.last_params.temperature == 0.3


def test_session_max_context_overrides_model_window(tmp_path):
    """本会话上限优先于模型窗口：小上限 + 大窗口模型 → 触发 context_overflow。"""
    from shared.envelope import SessionParams

    store = make_store(tmp_path)
    meta = store.create(None, None)
    store.update(
        meta.id, title="", note="", max_context=60, params=SessionParams()
    )
    emitted: list = []
    # 模型窗口很大（100 万），但本会话上限只有 60 → 仍应本地收口
    loop = AgentLoop(store, FakeGateway(ctx_window=1_000_000), emitted.append, tmp_path)
    loop.run_turn(meta.id, SendMessage(text="很长的问题" * 100))
    errors = [e for e in emitted if e.type == "error"]
    assert errors and errors[0].code == "context_overflow"


# -- 思考能力探测与折叠（spec rev25） -------------------------------------------


def test_loop_probes_once_before_calling(tmp_path):
    """未知模型：调用前先发一条 probing 预告（不落盘），再调用网关探测。"""
    store = make_store(tmp_path)
    meta = store.create(None, None)
    gw = FakeGateway(reasoning_pending=True)
    emitted: list = []
    loop = AgentLoop(store, gw, emitted.append, tmp_path)
    loop.run_turn(meta.id, SendMessage(text="hi"))

    probing = [e for e in emitted if e.type == "turn.status" and e.state == "probing"]
    assert probing and probing[0].note and "探测" in probing[0].note
    assert gw.probed == ["m1"]
    # 预告是瞬态事件，不进事件流（不进上下文，也不加重放负担）
    persisted = [e["type"] for e in store.replay(meta.id)]
    assert "turn.status" not in persisted


def test_loop_carries_reasoning_into_final(tmp_path):
    """思考增量与正文分流上报；AssistantFinal 与落盘都带 reasoning。"""
    store = make_store(tmp_path)
    meta = store.create(None, None)
    gw = FakeGateway(chunks=("答案",), reasoning_chunks=("先想", "再想"))
    emitted: list = []
    loop = AgentLoop(store, gw, emitted.append, tmp_path)
    loop.run_turn(meta.id, SendMessage(text="hi"))

    thoughts = [
        e for e in emitted if e.type == "msg.assistant.delta" and getattr(e, "reasoning", False)
    ]
    assert "".join(e.content for e in thoughts) == "先想再想"
    final = [e for e in emitted if e.type == "msg.assistant.final"][-1]
    assert final.reasoning == "先想再想"
    assert final.content == "答案"

    persisted = [e for e in store.replay(meta.id) if e["type"] == "msg.assistant.final"]
    assert persisted[-1]["payload"]["reasoning"] == "先想再想"


def test_final_usage_carries_timing_for_tps(tmp_path):
    """TPS 口径来源：Usage 的首 token / 末 token 时刻随事件流落盘，回放一致。"""
    from shared.envelope import Usage

    store = make_store(tmp_path)
    meta = store.create(None, None)

    class TimingGateway(FakeGateway):
        def stream_chat(
            self,
            session_id,
            turn_seq,
            model_id,
            messages,
            cancel_token,
            on_delta,
            on_reasoning=None,
            params=None,
            tools=None,
            on_tool_calls=None,
        ):
            on_delta("答")
            return Usage(
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
                first_token_ms=200,
                elapsed_ms=1200,
            )

    loop = AgentLoop(store, TimingGateway(), lambda e: None, tmp_path)
    loop.run_turn(meta.id, SendMessage(text="hi"))
    final = [e for e in store.replay(meta.id) if e["type"] == "msg.assistant.final"][-1]
    usage = final["payload"]["usage"]
    assert usage["first_token_ms"] == 200
    assert usage["elapsed_ms"] == 1200
    assert usage["completion_tokens"] == 20


# -- 会话记忆 / 压缩（spec v0.0.1） --------------------------------------------


MEMORY_MARK = "会话记忆："
MEMORY_REPLY = f"{MEMORY_MARK}\n- 目标：测试记忆压缩。\n- 关键事实：无。"


class MemoryGateway(FakeGateway):
    """可区分「记忆压缩调用」与「普通回合」：前者 system 含记忆提示词。"""

    def __init__(self, **kwargs):
        super().__init__(chunks=("回复" * 200,), **kwargs)
        self.last_system = ""

    def stream_chat(
        self,
        session_id,
        turn_seq,
        model_id,
        messages,
        cancel_token,
        on_delta,
        on_reasoning=None,
        params=None,
        tools=None,
        on_tool_calls=None,
    ):
        self.last_messages = [dict(m) for m in messages]
        self.last_system = messages[0]["content"] if messages else ""
        text = MEMORY_REPLY if "记忆整理器" in self.last_system else "回复" * 200
        on_delta(text)
        return Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)


def test_plan_memory_keeps_tail_by_token_budget():
    """选择面按 token 预算保留近段（不是固定最近 N 轮）。"""
    from core.agent.memory import plan_memory

    events = []
    for i in range(6):
        events.append({"seq": i * 2, "type": "msg.user", "payload": {"text": "问" * 300}})
        events.append(
            {"seq": i * 2 + 1, "type": "msg.assistant.final", "payload": {"content": "答" * 300}}
        )
    plan = plan_memory(events, "", covered_seq=-1, keep_budget=100)
    assert plan is not None
    assert 0 <= plan.covered_seq < 11, "更早的消息被概括"
    assert plan.transcript, "转录非空"
    # 最后一条事件（seq=11）作为近段保留，不进入记忆
    assert plan.covered_seq < 11


def test_plan_memory_force_keeps_only_last_turn():
    """强求压缩（rev35）：不按预算保留，只留最后 1 轮问答；不足两轮则无内容。"""
    from core.agent.memory import plan_memory

    events = []
    for i in range(3):
        events.append({"seq": i * 2, "type": "msg.user", "payload": {"text": f"问{i}"}})
        events.append(
            {"seq": i * 2 + 1, "type": "msg.assistant.final", "payload": {"content": f"答{i}"}}
        )

    # 预算极大：常规压缩无内容可压（整段都在尾部保留内）
    assert plan_memory(events, "", covered_seq=-1, keep_budget=10**9) is None

    plan = plan_memory(events, "", covered_seq=-1, keep_budget=10**9, force=True)
    assert plan is not None
    assert plan.covered_seq == 3, "只概括到倒数第 2 条（保留最后 1 轮）"
    assert "问0" in plan.transcript and "问2" not in plan.transcript

    # 仅 1 轮问答：强求也无内容可概括（保留最后那一轮）
    one_turn = events[:2]
    assert plan_memory(one_turn, "", covered_seq=-1, keep_budget=10**9, force=True) is None


def test_compress_memory_writes_file_and_replaces_history(tmp_path):
    store = make_store(tmp_path)
    meta = store.create(None, None)
    gw = MemoryGateway(ctx_window=8000)
    emitted: list = []
    loop = AgentLoop(store, gw, emitted.append, tmp_path)
    for i in range(6):
        loop.run_turn(meta.id, SendMessage(text=f"问题{i}"))

    loop.compress_memory(meta.id)

    results = [e for e in emitted if e.type == "session.memory.result"]
    assert results and results[-1].ok, results[-1].error if results else "no result"
    assert results[-1].recommended_max > 0, "推荐范围随结果下发"
    memory = store.read_memory(meta.id)
    assert memory is not None and memory.revision == 1 and memory.covered_seq >= 0
    assert MEMORY_MARK in store.read_memory_text(meta.id)

    before = len([e for e in store.replay(meta.id) if e["type"] == "msg.user"])
    loop.run_turn(meta.id, SendMessage(text="追加一问"))
    # 记忆进入 system 段，历史段只保留 covered_seq 之后的消息
    assert MEMORY_MARK in gw.last_system
    history_msgs = [m for m in gw.last_messages if m["role"] != "system"]
    assert len(history_msgs) < before * 2


def test_compress_memory_force_builds_even_short_chat(tmp_path):
    """强求压缩（rev35 用户反馈修复）：短对话常规无内容可压，按钮强求仍能建立记忆。"""
    store = make_store(tmp_path)
    meta = store.create(None, None)
    emitted: list = []
    loop = AgentLoop(store, MemoryGateway(ctx_window=8000), emitted.append, tmp_path)
    for i in range(2):
        loop.run_turn(meta.id, SendMessage(text=f"问题{i}"))

    loop.compress_memory(meta.id)  # 常规：近段整段在保留预算内 → 无内容
    results = [e for e in emitted if e.type == "session.memory.result"]
    assert results and not results[-1].ok, results[-1].error if results else "no result"
    assert store.read_memory(meta.id) is None

    emitted.clear()
    loop.compress_memory(meta.id, force=True)  # 强求：只留最后 1 轮
    results = [e for e in emitted if e.type == "session.memory.result"]
    assert results and results[-1].ok, results[-1].error if results else "no result"
    memory = store.read_memory(meta.id)
    assert memory is not None and memory.revision == 1
    assert MEMORY_MARK in store.read_memory_text(meta.id)


def test_compress_memory_without_content_reports_error(tmp_path):
    store = make_store(tmp_path)
    meta = store.create(None, None)
    emitted: list = []
    loop = AgentLoop(store, MemoryGateway(), emitted.append, tmp_path)
    loop.compress_memory(meta.id)
    results = [e for e in emitted if e.type == "session.memory.result"]
    assert results and not results[-1].ok
    assert "无可压缩" in (results[-1].error or "")
    assert store.read_memory(meta.id) is None


def test_compress_memory_gateway_failure_keeps_state(tmp_path):
    from core.gateway.errors import GatewayError

    store = make_store(tmp_path)
    meta = store.create(None, None)

    class BrokenGateway(MemoryGateway):
        def stream_chat(self, session_id, turn_seq, model_id, messages, cancel_token, on_delta, on_reasoning=None, params=None, tools=None, on_tool_calls=None):
            if "记忆整理器" in (messages[0]["content"] if messages else ""):
                raise GatewayError("上游拒绝", code="protocol_error")
            return super().stream_chat(
                session_id, turn_seq, model_id, messages, cancel_token, on_delta, on_reasoning, params
            )

    gw = BrokenGateway(ctx_window=8000)
    loop = AgentLoop(store, gw, lambda e: None, tmp_path)
    for i in range(6):
        loop.run_turn(meta.id, SendMessage(text=f"问题{i}"))
    emitted: list = []
    loop.emit = emitted.append
    loop.compress_memory(meta.id)
    results = [e for e in emitted if e.type == "session.memory.result"]
    assert results and not results[-1].ok
    assert store.read_memory(meta.id) is None, "失败不得写入记忆"


def test_compress_memory_failure_message_is_redacted(tmp_path):
    """`session.memory.result.error` 是回显通道，上游异常文本须过统一脱敏。"""
    from core.gateway.errors import GatewayError
    from shared.redact import MASK

    store = make_store(tmp_path)
    meta = store.create(None, None)

    class KeyLeakGateway(MemoryGateway):
        def stream_chat(self, session_id, turn_seq, model_id, messages, cancel_token, on_delta, on_reasoning=None, params=None, tools=None, on_tool_calls=None):
            if "记忆整理器" in (messages[0]["content"] if messages else ""):
                raise GatewayError("认证失败 api_key=sk-deadbeefcafe1234", code="auth_error")
            return super().stream_chat(
                session_id, turn_seq, model_id, messages, cancel_token, on_delta, on_reasoning, params
            )

    gw = KeyLeakGateway(ctx_window=8000)
    loop = AgentLoop(store, gw, lambda e: None, tmp_path)
    for i in range(6):
        loop.run_turn(meta.id, SendMessage(text=f"问题{i}"))
    emitted: list = []
    loop.emit = emitted.append
    loop.compress_memory(meta.id)
    results = [e for e in emitted if e.type == "session.memory.result"]
    assert results and not results[-1].ok
    assert "sk-deadbeefcafe1234" not in (results[-1].error or "")
    assert MASK in (results[-1].error or "")


def test_compress_disabled_reports_error_and_writes_nothing(tmp_path):
    """会话级「压缩记忆」关闭时：按钮触发也尊重该选择，ok=False 且不写文件。"""
    from shared.envelope import SessionParams

    store = make_store(tmp_path)
    meta = store.create(None, None)
    store.update(
        meta.id,
        title="T",
        note="",
        max_context=None,
        params=SessionParams(),
        memory_use=True,
        memory_compress=False,
        memory_threshold=None,
    )
    emitted: list = []
    loop = AgentLoop(store, MemoryGateway(ctx_window=8000), emitted.append, tmp_path)
    loop.compress_memory(meta.id)
    results = [e for e in emitted if e.type == "session.memory.result"]
    assert results and not results[-1].ok
    assert "已关闭记忆压缩" in (results[-1].error or "")
    assert store.read_memory(meta.id) is None
    assert not store._memory_md_path(meta.id).exists()


def test_compress_memory_cancel_writes_nothing(tmp_path):
    """spec §5：summarizing 期间 CancelTurn → interrupted，不写记忆文件。"""
    store = make_store(tmp_path)
    meta = store.create(None, None)
    holder: dict = {}

    class CancellingGateway(MemoryGateway):
        def stream_chat(
            self,
            session_id,
            turn_seq,
            model_id,
            messages,
            cancel_token,
            on_delta,
            on_reasoning=None,
            params=None,
            tools=None,
            on_tool_calls=None,
        ):
            if "记忆整理器" in (messages[0]["content"] if messages else ""):
                holder["loop"].cancel(session_id)
            return super().stream_chat(
                session_id, turn_seq, model_id, messages, cancel_token, on_delta, on_reasoning, params
            )

    gw = CancellingGateway(ctx_window=8000)
    emitted: list = []
    loop = AgentLoop(store, gw, emitted.append, tmp_path)
    holder["loop"] = loop
    for i in range(6):
        loop.run_turn(meta.id, SendMessage(text=f"问题{i}"))

    emitted.clear()
    loop.compress_memory(meta.id)

    results = [e for e in emitted if e.type == "session.memory.result"]
    assert results and not results[-1].ok
    assert "取消" in (results[-1].error or "")
    assert store.read_memory(meta.id) is None, "取消不得写入记忆"
    assert not store._memory_md_path(meta.id).exists()
    assert "interrupted" in [e.state for e in emitted if e.type == "turn.status"]


def test_auto_compress_gated_by_switches_and_threshold(tmp_path):
    """M5：仅 `use & compress & auto` 且输入侧占用 ≥ 阈值时，回合结束后自动压缩一次。"""
    from shared.envelope import ContextUsage, SessionParams

    def run_case(tag: str, *, auto: bool, threshold: int, total: int, window: int = 1000):
        store = make_store(tmp_path / tag)
        meta = store.create(None, None)
        store.update(
            meta.id,
            title="T",
            note="",
            max_context=window,
            params=SessionParams(),
            memory_use=True,
            memory_compress=True,
            memory_auto=auto,
            memory_threshold=threshold,
        )
        for _ in range(6):
            store.append_event(meta.id, SendMessage(text="问题" * 200))
            store.append_event(meta.id, AssistantFinal(content="回答" * 200))
        gw = MemoryGateway(ctx_window=window)
        emitted: list = []
        loop = AgentLoop(store, gw, emitted.append, tmp_path / tag)
        # 注入装配用量（total 含 reserve），隔离阈值判定
        loop._usage_by_session[meta.id] = ContextUsage(
            segments={"reserve": window // 8}, total=total, window=window
        )
        loop._maybe_auto_compress(meta.id, store.get_meta(meta.id))
        results = [e for e in emitted if e.type == "session.memory.result"]
        return store, meta, results

    # auto 开 + 占用 90% ≥ 阈值 50% → 自动压缩
    store, meta, results = run_case("on", auto=True, threshold=50, total=900)
    assert results and results[-1].ok, results
    assert store.read_memory(meta.id) is not None
    assert store._memory_md_path(meta.id).exists()

    # auto 关 → 不自动压缩
    store, meta, results = run_case("off", auto=False, threshold=50, total=900)
    assert results == []
    assert store.read_memory(meta.id) is None

    # 占用 40% < 阈值 50% → 不自动压缩
    store, meta, results = run_case("low", auto=True, threshold=50, total=400)
    assert results == []
    assert store.read_memory(meta.id) is None


def test_branch_revert_and_switch(tmp_path):
    """rev31：分支只引用 seq（不复制事件），回退只移游标，主干不被其它分支污染。"""
    store = make_store(tmp_path)
    sid = store.create("分支", None).id
    for i in range(3):
        store.append_event(sid, SendMessage(text=f"q{i}"))
        store.append_event(sid, AssistantFinal(content=f"a{i}"))
    users = [e for e in store.replay(sid) if e.get("type") == "msg.user"]
    q1 = users[1]["seq"]

    graph = store.create_branch(sid, q1)
    assert [b.id for b in graph.branches] == ["br0", "br1"]
    assert graph.active == "br1"
    assert [
        e["payload"]["text"] for e in store.replay(sid) if e.get("type") == "msg.user"
    ] == ["q0", "q1"]

    store.append_event(sid, SendMessage(text="q-new"))
    store.append_event(sid, AssistantFinal(content="a-new"))
    assert [
        e["payload"]["text"] for e in store.replay(sid) if e.get("type") == "msg.user"
    ] == ["q0", "q1", "q-new"]

    # 切回主干：其它分支追加的事件不污染主干
    store.switch_branch(sid, "br0")
    assert [
        e["payload"]["text"] for e in store.replay(sid) if e.get("type") == "msg.user"
    ] == ["q0", "q1", "q2"]

    # 回退：游标移到该轮之前，尾部 seq 保留（len > cursor）
    store.revert_to(sid, q1)
    assert [
        e["payload"]["text"] for e in store.replay(sid) if e.get("type") == "msg.user"
    ] == ["q0"]
    assert store.turn_count(sid) == 1
    br0 = store.list_branches(sid).branches[0]
    assert br0.cursor == 4 and len(br0.events) > 4

    # 切回 br1 仍完整
    store.switch_branch(sid, "br1")
    assert [
        e["payload"]["text"] for e in store.replay(sid) if e.get("type") == "msg.user"
    ] == ["q0", "q1", "q-new"]

    infos = {b.id: b for b in store.branch_info(sid)}
    assert infos["br1"].active and infos["br1"].turns == 3
    assert infos["br0"].parent is None and infos["br1"].parent == "br0"


def test_branch_limit_and_unknown(tmp_path):
    """rev31：分支上限 5；未知分支/未知 seq 一律 KeyError。"""
    store = make_store(tmp_path)
    sid = store.create("上限", None).id
    store.append_event(sid, SendMessage(text="q0"))
    base = [e for e in store.replay(sid) if e.get("type") == "msg.user"][0]["seq"]
    for _ in range(4):  # br1..br4
        store.create_branch(sid, base)
    assert len(store.list_branches(sid).branches) == 5
    with pytest.raises(ValueError):
        store.create_branch(sid, base)
    with pytest.raises(KeyError):
        store.switch_branch(sid, "brX")
    with pytest.raises(KeyError):
        store.revert_to(sid, 999_999)


def test_per_branch_memory_isolated_and_copied(tmp_path):
    """rev31：记忆按分支隔离；分叉时父记忆快照给子分支，互不覆盖。"""
    store = make_store(tmp_path)
    sid = store.create("记忆", None).id
    store.append_event(sid, SendMessage(text="q0"))
    q0 = [e for e in store.replay(sid) if e.get("type") == "msg.user"][0]["seq"]
    store.write_memory(sid, covered_seq=q0, model="m", tokens_est=5, text="记忆A")
    assert store.read_memory_text(sid) == "记忆A"

    store.append_event(sid, SendMessage(text="q1"))
    q1 = [e for e in store.replay(sid) if e.get("type") == "msg.user"][1]["seq"]
    store.create_branch(sid, q1)  # 父记忆 covered_seq=q0 <= q1 → 继承
    assert store.read_memory_text(sid) == "记忆A"

    store.write_memory(sid, covered_seq=q1, model="m", tokens_est=6, text="记忆B")
    store.switch_branch(sid, "br0")
    assert store.read_memory_text(sid) == "记忆A"


def test_memory_history_after_two_compressions(tmp_path):
    """两次写入：上一版归档到 memory/history/<rev>.md，可列可读（不注入）。"""
    store = make_store(tmp_path)
    sid = store.create("记忆", None).id
    store.write_memory(sid, covered_seq=1, model="m", tokens_est=5, text="第一版")
    first = store.read_memory_text(sid)
    memory = store.write_memory(sid, covered_seq=3, model="m", tokens_est=6, text="第二版")
    assert memory.revision == 2
    assert store.read_memory_text(sid) == "第二版"
    assert store.list_memory_history(sid) == [1]
    assert store.read_memory_history(sid, 1) == first
    assert store.read_memory_history(sid, 99) == ""  # 不存在的版本 → 空串


def test_legacy_summary_read_as_memory(tmp_path):
    """旧 rev26 布局（`sessions/<id>/summary.*`）仍可读作记忆（br0 回退）。"""
    import json

    from shared.schema import SessionMemory

    store = make_store(tmp_path)
    sid = store.create("旧", None).id
    session_dir = store.session_dir(sid)
    legacy = SessionMemory(revision=3, covered_seq=7, model="old", tokens_est=9)
    (session_dir / "summary.json").write_text(
        json.dumps(legacy.model_dump(mode="json")), encoding="utf-8"
    )
    (session_dir / "summary.md").write_text("旧版记忆正文", encoding="utf-8")
    memory = store.read_memory(sid)
    assert memory is not None and memory.revision == 3 and memory.covered_seq == 7
    assert store.read_memory_text(sid) == "旧版记忆正文"


def test_effective_threshold_user_value_clamped():
    from datetime import datetime, timezone

    from shared.envelope import SessionMeta
    from shared.schema import MemoryConfig

    from core.agent.memory import effective_threshold

    now = datetime.now(timezone.utc)
    config = MemoryConfig(threshold=90)
    plain = SessionMeta(id="s", title="t", created_at=now, updated_at=now)
    assert effective_threshold(plain, config) == 90  # 未覆盖 → 全局默认
    overridden = SessionMeta(
        id="s", title="t", created_at=now, updated_at=now, memory_threshold=75
    )
    assert effective_threshold(overridden, config) == 75  # 用户指定优先
    clamped = SessionMeta(
        id="s", title="t", created_at=now, updated_at=now, memory_threshold=10
    )
    assert effective_threshold(clamped, config) == 50  # 夹到合法区间


def test_effective_switches_session_override_wins():
    from datetime import datetime, timezone

    from shared.envelope import SessionMeta
    from shared.schema import MemoryConfig

    from core.agent.memory import effective_switches

    now = datetime.now(timezone.utc)
    config = MemoryConfig(use=True, compress=True, auto=False)
    plain = SessionMeta(id="s", title="t", created_at=now, updated_at=now)
    assert effective_switches(plain, config) == (True, True, False)  # None → 跟随全局
    off = SessionMeta(
        id="s", title="t", created_at=now, updated_at=now, memory_use=False, memory_compress=False
    )
    assert effective_switches(off, config) == (False, False, False)  # 会话覆盖优先
    mixed = SessionMeta(
        id="s", title="t", created_at=now, updated_at=now, memory_use=False, memory_compress=None
    )
    assert effective_switches(mixed, config) == (False, True, False)  # 逐项覆盖
    auto_on = SessionMeta(
        id="s", title="t", created_at=now, updated_at=now, memory_auto=True
    )
    assert effective_switches(auto_on, config) == (True, True, True)  # 会话开自动


def test_recommended_range_by_target_ratio():
    """窗口 <=0 → (0,0)；否则 max=窗口//target_ratio，min=max//2。"""
    from core.agent.memory import recommended_range
    from shared.schema import MemoryConfig

    config = MemoryConfig(target_ratio=16)
    assert recommended_range(0, config) == (0, 0)
    assert recommended_range(-5, config) == (0, 0)
    assert recommended_range(32000, config) == (1000, 2000)
