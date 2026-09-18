"""core.agent 单元测试（上下文 / 会话存储 / 回合循环）。"""

from __future__ import annotations

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
    def __init__(self, chunks=("你", "好"), slots=None, cancel_after=None, ctx_window=1000):
        self._chunks = list(chunks)
        self._slots = slots if slots is not None else {"main": "m1"}
        self._cancel_after = cancel_after
        self._ctx_window = ctx_window

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
        history_turns=20,
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
    config = ConfigSnapshot(system_prompt="", memory="", reserve=4096, history_turns=20)
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
    config = ConfigSnapshot(system_prompt="S", memory="", reserve=100, history_turns=20)
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
