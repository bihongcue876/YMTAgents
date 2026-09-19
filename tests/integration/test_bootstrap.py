"""启动装配与请求分派集成测试（core 侧，无 GUI）。"""

from __future__ import annotations

from types import SimpleNamespace

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import (
    AssistantFinal,
    BranchSession,
    FetchModels,
    ModelSpec,
    NewSession,
    PersonaDelete,
    PersonaSave,
    PersonaSwitch,
    ProviderSpec,
    ProviderUpsert,
    ResumeSession,
    RevertSession,
    SendMessage,
    SetSlot,
    SettingsUpdate,
    SummarizeSession,
    SwitchBranch,
    SwitchModel,
    TestConnection,
)
from tests.mocks.gateway import MockGateway


def _boot(tmp_path, monkeypatch, gateway: MockGateway):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=lambda store: gateway)


def test_bootstrap_initial_and_dispatch(tmp_path, monkeypatch, qapp):
    gateway = MockGateway()
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.push_initial_state()
        types = [e.type for e in events]
        assert {"provider.list", "session.index", "health.report"} <= set(types)

        events.clear()
        ctx.controller.handle(NewSession(title="T"))
        assert any(e.type == "session.created" for e in events)
        assert any(e.type == "session.index" for e in events)
        session_id = ctx.controller.current_session_id
        assert session_id

        events.clear()
        ctx.controller.handle(SendMessage(text="你好"))
        types = [e.type for e in events]
        assert "msg.assistant.delta" in types
        statuses = [e for e in events if e.type == "turn.status"]
        assert statuses and statuses[-1].state == "done"
        assert types[-1] == "session.detail.result", "rev24：回合结束后刷新会话详情"

        # 回放
        events.clear()
        ctx.controller.handle(ResumeSession(session_id=session_id))
        replay = [e for e in events if e.type == "session.events"][0]
        assert any(ev["type"] == "msg.user" for ev in replay.events)

        # 切换模型落盘 model.switch
        ctx.controller.handle(SwitchModel(slot="main", model_id="mock-model"))
        assert any(ev["type"] == "model.switch" for ev in ctx.session_store.replay(session_id))
    finally:
        ctx.worker.stop()


def test_bootstrap_timeout(tmp_path, monkeypatch, qapp):
    gateway = MockGateway(timeout=True)
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(NewSession())
        events.clear()
        ctx.controller.handle(SendMessage(text="hi"))
        assert any(e.type == "error" for e in events)
        statuses = [e for e in events if e.type == "turn.status"]
        assert statuses and statuses[-1].state == "failed"
    finally:
        ctx.worker.stop()


# -- 全局槽位绑定接线（spec rev4） ------------------------------------------


def test_slot_set_dispatch_syncs_providers_and_health(tmp_path, monkeypatch, qapp):
    """SetSlot 写全局槽位：ProviderList.slots 与 HealthReport 同步回发。"""
    gateway = MockGateway(slots={"main": None})
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(SetSlot(slot="main", model_id="mock-model"))
        providers = [e for e in events if e.type == "provider.list"][-1]
        health = [e for e in events if e.type == "health.report"][-1]
        assert providers.slots["main"] == "mock-model"
        assert health.main_model == "mock-model"
        assert health.slot_ready is True

        events.clear()
        ctx.controller.handle(SetSlot(slot="main", model_id=None))
        health = [e for e in events if e.type == "health.report"][-1]
        assert health.main_model is None
        assert health.slot_ready is False
    finally:
        ctx.worker.stop()


def test_switch_without_session_records_last_used(tmp_path, monkeypatch, qapp):
    """rev14/rev23：**无会话**时切换模型 = 设置新对话的默认（仍登记 slots.main）。

    rev23 修订：会话内切换不再登记上次使用（单对话选择，见 test_in_session_switch_stays_in_session）。
    """
    gateway = MockGateway(slots={"main": None})
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(SwitchModel(slot="main", model_id="mock-model"))
        providers = [e for e in events if e.type == "provider.list"][-1]
        assert providers.slots["main"] == "mock-model"

        ctx.controller.handle(NewSession(title="L"))
        ctx.controller.handle(SendMessage(text="hi"))
        assert gateway.calls[-1]["model_id"] == "mock-model"
    finally:
        ctx.worker.stop()


def test_in_session_switch_stays_in_session(tmp_path, monkeypatch, qapp):
    """rev23：对话内切换模型**只属于该对话**，不再登记「上次使用」。

    用户裁决（对齐 Coding agents 平台）：单对话选择、单对话不一致；
    新对话的默认由全局默认决定，不随会话内选择漂移。
    """
    gateway = MockGateway(slots={"main": "mock-model"})
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        # 给端点补一个可切换的第二模型
        ctx.controller.handle(
            ProviderUpsert(
                provider=ProviderSpec(
                    id="prv_mock",
                    name="Mock",
                    base_url="https://mock.local",
                    models=[ModelSpec(id="mock-model", ctx_window=8192), ModelSpec(id="m2", ctx_window=8192)],
                ),
                api_key=None,
            )
        )
        ctx.controller.handle(NewSession(title="A"))
        events.clear()
        ctx.controller.handle(SwitchModel(slot="main", model_id="m2"))
        providers = [e for e in events if e.type == "provider.list"][-1]
        assert providers.slots["main"] == "mock-model", "会话内切换不得改写全局默认"
        meta = ctx.session_store.get_meta(ctx.controller.current_session_id)
        assert meta.main_model == "m2", "会话级选择生效"
    finally:
        ctx.worker.stop()


def test_persona_end_to_end(tmp_path, monkeypatch, qapp):
    """阶段 2 第一片端到端：保存角色 → 会话切换 → 回合 system 段 = 该角色 prompt。"""
    gateway = MockGateway()
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.push_initial_state()
        assert any(e.type == "persona.list" for e in events)

        # 保存角色（无 id = 新建）
        ctx.controller.handle(PersonaSave(name="评审员", prompt="你是严格的代码评审员。"))
        listing = [e for e in events if e.type == "persona.list"][-1]
        created = [p for p in listing.personas if p.name == "评审员"]
        assert created and not created[0].is_default

        # 新会话默认用全局默认角色（YMT）；切换到评审员后回合 system 段 = 评审员 prompt
        ctx.controller.handle(NewSession(title="P"))
        ctx.controller.handle(SendMessage(text="hi"))
        ymt_system = gateway.calls[-1]["messages"][0]["content"]
        assert "言明通" in ymt_system  # YMT 预置兜底

        ctx.controller.handle(PersonaSwitch(persona_id=created[0].id))
        ctx.controller.handle(SendMessage(text="评审一下"))
        custom_system = gateway.calls[-1]["messages"][0]["content"]
        # system 段 = 角色 prompt 开头 + 环境陈述（组装顺序：system → files → env）
        assert custom_system.startswith("你是严格的代码评审员。")

        # 会话记录了自己的角色（单对话选择、单对话不一致）
        meta = ctx.session_store.get_meta(ctx.controller.current_session_id)
        assert meta.persona_id == created[0].id

        # 删除被会话引用的角色 → 装配回退 YMT，不空转
        ctx.controller.handle(PersonaDelete(persona_id=created[0].id))
        ctx.controller.handle(SendMessage(text="还在吗"))
        fallback_system = gateway.calls[-1]["messages"][0]["content"]
        assert "言明通" in fallback_system
    finally:
        ctx.worker.stop()


def test_persona_delete_builtin_rejected(tmp_path, monkeypatch, qapp):
    """YMT 预置角色不可删除（invalid_request，而非静默忽略）。"""
    from core.agent.persona import YMT_PERSONA_ID

    gateway = MockGateway()
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(PersonaDelete(persona_id=YMT_PERSONA_ID))
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "invalid_request"
        assert ctx.controller.personas.get(YMT_PERSONA_ID) is not None
    finally:
        ctx.worker.stop()


def test_unbound_slot_fails_turn_with_model_unbound(tmp_path, monkeypatch, qapp):
    """全局与会话级均未绑定时，回合以 model_unbound 失败而非静默（spec rev5 §2）。

    回归锚点：此处此前误报 provider_not_found —— 「未绑定」不是「找不到」。
    """
    gateway = MockGateway(slots={"main": None})
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(NewSession())
        events.clear()
        ctx.controller.handle(SendMessage(text="hi"))
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "model_unbound"
        assert "模型配置" in errors[0].message  # 文案必须可操作
        statuses = [e for e in events if e.type == "turn.status"]
        assert statuses and statuses[-1].state == "failed"
    finally:
        ctx.worker.stop()


def test_settings_update_and_test_connection_dispatch(tmp_path, monkeypatch, qapp):
    """回归锚点：settings.update → gateway.reload_settings；provider.test → gateway.test_connection。

    这两个方法此前**未在 IModelGateway 中声明**，controller 却直接调用，
    导致 Mock 环境下「改设置」与「测试连接」必然 AttributeError —— 两条路径长期无测试覆盖。
    """
    gateway = MockGateway(test_result=(False, None, "model_not_found"))
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(SettingsUpdate(section="ui", data={"theme": "dark"}))
        state = [e for e in events if e.type == "settings.state"][-1]
        assert state.data["ui"]["theme"] == "dark"

        events.clear()
        ctx.controller.handle(TestConnection(provider_id="prv_mock", model_id="mock-model"))
        result = [e for e in events if e.type == "provider.test.result"][-1]
        assert result.ok is False
        assert result.error == "model_not_found"
    finally:
        ctx.worker.stop()


# -- 静默错误收口（spec rev8 §3–§5） ----------------------------------------


def test_non_main_session_switch_is_rejected_and_leaves_main_intact(tmp_path, monkeypatch, qapp):
    """回归锚点：会话级切换非 main 槽位不得污染 `main_model`。

    实证：`set_model` 忽略 slot 参数，任何槽位都写 `meta.main_model` ——
    切 thinking 会把主模型改掉，且 `model.switch` 事件声称的槽位与生效对象不一致。
    """
    gateway = MockGateway(slots={"main": None})
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(NewSession())
        sid = ctx.controller.current_session_id
        events.clear()
        ctx.controller.handle(SwitchModel(slot="thinking", model_id="m-thinking"))

        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "invalid_request"
        assert "thinking" in errors[0].message  # 说明白哪个槽位不能用
        assert ctx.session_store.get_meta(sid).main_model is None  # main 未被污染
        assert not any(e["type"] == "model.switch" for e in ctx.session_store.replay(sid))
    finally:
        ctx.worker.stop()


def test_invalid_settings_value_reports_invalid_request(tmp_path, monkeypatch, qapp):
    """A10：schema 不符必须回 `invalid_request`，且既有配置保持不变。

    此前该异常从 controller 直抛、被核心线程整条吞掉：既不落盘也无提示。
    """
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(SettingsUpdate(section="ui", data={"theme": "blue"}))
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "invalid_request"
        assert "ui" in errors[0].message
        assert ctx.config_store.load("settings").ui.theme == "light"  # 原值保持
        assert not any(e.type == "settings.state" for e in events)  # 不推坏状态
    finally:
        ctx.worker.stop()


def test_settings_write_failure_is_reported(tmp_path, monkeypatch, qapp):
    """落盘失败必须上报 `storage_error`（该码此前零发射，只在日志留痕）。"""
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    events: list = []
    ctx.bridge.event_received.connect(events.append)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    try:
        monkeypatch.setattr(ctx.config_store, "save", boom)
        ctx.controller.handle(SettingsUpdate(section="ui", data={"theme": "dark"}))
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "storage_error"
        assert "保存失败" in errors[0].message
        assert not any(e.type == "settings.state" for e in events)
    finally:
        ctx.worker.stop()


def test_provider_write_failure_is_reported(tmp_path, monkeypatch, qapp):
    """供应商写入失败（含凭据管理器不可用）同样上报，不得静默。"""
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    events: list = []
    ctx.bridge.event_received.connect(events.append)

    def boom(*_args, **_kwargs):
        raise RuntimeError("keyring unavailable")

    try:
        monkeypatch.setattr(ctx.gateway, "upsert_provider", boom)
        ctx.controller.handle(
            ProviderUpsert(
                provider=ProviderSpec(id="prv_z", name="Z", base_url="https://api.z.com/v1")
            )
        )
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "storage_error"
        assert "凭据管理器" in errors[0].message
    finally:
        ctx.worker.stop()


# -- 未知请求不再静默（spec rev9 §1） ----------------------------------------


def test_unknown_request_type_is_reported_not_silent(tmp_path, monkeypatch, qapp):
    """回归锚点：未知请求类型此前只写一条 warning，调用方拿不到任何反馈。"""
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(SimpleNamespace(type="bogus.request"))
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "invalid_request"
        assert "未知请求类型" in errors[0].message
    finally:
        ctx.worker.stop()


def test_bridge_rejects_non_envelope_object(tmp_path, monkeypatch, qapp):
    """非 dict、也非任一信封类型的对象必须回 invalid_request，而不是静默入队后被丢弃。"""
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.bridge.submit("这不是信封")  # type: ignore[arg-type]
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "invalid_request"
        assert "str" in (errors[0].detail or "")
    finally:
        ctx.worker.stop()


def test_error_detail_is_redacted(tmp_path, monkeypatch, qapp):
    """错误详情一律脱敏：`error` 事件会落进 events.jsonl，夹带密钥即等于持久化泄漏。"""
    from shared.redact import MASK

    secret = "sk-LEAK1234567890"
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        # 让「失败字段本身就是密钥」：pydantic 会把它写进错误文本，脱敏层必须拦下
        ctx.controller.handle(SettingsUpdate(section="ui", data={"theme": [secret]}))
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "invalid_request"
        assert secret not in (errors[0].detail or "")
        assert MASK in (errors[0].detail or "")
    finally:
        ctx.worker.stop()


# -- 端点模型列表（spec rev9 §2） -------------------------------------------


def test_fetch_models_dispatch(tmp_path, monkeypatch, qapp):
    """`provider.models` → `provider.models.result`：只读探测，不写任何配置。"""
    gateway = MockGateway(remote_models=(True, ["a-model", "b-model"], None))
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    before = ctx.config_store.load("models").model_dump()
    try:
        ctx.controller.handle(FetchModels(provider_id="prv_mock"))
        result = [e for e in events if e.type == "provider.models.result"][-1]
        assert result.ok is True
        assert result.models == ["a-model", "b-model"]
        assert ctx.config_store.load("models").model_dump() == before  # 未改配置
    finally:
        ctx.worker.stop()


def test_fetch_models_failure_is_reported(tmp_path, monkeypatch, qapp):
    gateway = MockGateway(remote_models=(False, [], "auth_error"))
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(FetchModels(provider_id="prv_mock"))
        result = [e for e in events if e.type == "provider.models.result"][-1]
        assert result.ok is False and result.error == "auth_error"
    finally:
        ctx.worker.stop()


def test_shutdown_ends_current_session(tmp_path, monkeypatch, qapp):
    """退出收口：结束当前会话并 fsync（docs 03 §10）。"""
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    try:
        ctx.controller.handle(NewSession())
        sid = ctx.controller.current_session_id
        ctx.controller.shutdown()
        events = ctx.session_store.replay(sid)
        assert events[-1]["type"] == "session.end"
        assert events[-1]["payload"]["reason"] == "app_exit"
    finally:
        ctx.worker.stop()


def test_shutdown_without_session_is_safe(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    try:
        ctx.controller.shutdown()  # 无当前会话：不得抛
    finally:
        ctx.worker.stop()


# -- 历史摘要化 / 压缩（spec rev26） -------------------------------------------


def test_summarize_dispatch_writes_summary_and_refreshes_detail(tmp_path, monkeypatch, qapp):
    """`session.summarize` → `session.summary.result` + 详情回推带摘要状态。"""
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(NewSession(title="压缩"))
        sid = ctx.controller.current_session_id
        # 直接落盘一段足够长的历史（超出尾部保留预算，保证有可压缩内容）
        for i in range(6):
            ctx.session_store.append_event(sid, SendMessage(text=f"问题{i}" + "细" * 300))
            ctx.session_store.append_event(sid, AssistantFinal(content="答" * 300))
        events.clear()

        ctx.controller.handle(SummarizeSession(session_id=sid))

        results = [e for e in events if e.type == "session.summary.result"]
        assert results and results[-1].ok, results[-1].error if results else "no result"
        assert ctx.session_store.read_summary(sid) is not None
        assert ctx.session_store.read_summary_text(sid).strip()
        detail = [e for e in events if e.type == "session.detail.result"][-1]
        assert detail.summary_revision == 1
        assert detail.summary_covered_seq >= 0
        assert detail.summary_threshold == 90  # 未覆盖 → 全局默认
    finally:
        ctx.worker.stop()


def test_branch_revert_switch_dispatch(tmp_path, monkeypatch, qapp):
    """rev31：session.branch / switch_branch / revert 分派 → 回推分支树与活动转录。"""
    ctx = _boot(tmp_path, monkeypatch, MockGateway())
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    try:
        ctx.controller.handle(NewSession(title="分支"))
        sid = ctx.controller.current_session_id
        for i in range(3):
            ctx.session_store.append_event(sid, SendMessage(text=f"问题{i}"))
            ctx.session_store.append_event(sid, AssistantFinal(content=f"答{i}"))
        ctx.controller.handle(ResumeSession(session_id=sid))
        events.clear()
        users = [e for e in ctx.session_store.replay(sid) if e.get("type") == "msg.user"]
        q1 = users[1]["seq"]

        ctx.controller.handle(BranchSession(session_id=sid, from_seq=q1))
        branches = [e for e in events if e.type == "session.branches"]
        assert branches and branches[-1].active == "br1"
        assert [b.id for b in branches[-1].branches] == ["br0", "br1"]
        assert any(e.type == "session.events" for e in events)

        events.clear()
        ctx.controller.handle(SwitchBranch(session_id=sid, branch_id="br0"))
        assert [e for e in events if e.type == "session.branches"][-1].active == "br0"

        events.clear()
        ctx.controller.handle(RevertSession(session_id=sid, to_seq=q1))
        assert ctx.session_store.turn_count(sid) == 1  # 回退到第 2 问之前
        detail = [e for e in events if e.type == "session.detail.result"]
        assert detail and detail[-1].branch_count == 2
        assert detail[-1].active_branch == "br0"

        events.clear()
        ctx.controller.handle(SwitchBranch(session_id=sid, branch_id="brX"))
        assert any(e.type == "error" for e in events)  # 未知分支 → 无效请求
    finally:
        ctx.worker.stop()
