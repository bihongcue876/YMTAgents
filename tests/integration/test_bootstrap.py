"""启动装配与请求分派集成测试（core 侧，无 GUI）。"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import (
    NewSession,
    ResumeSession,
    SendMessage,
    SetSlot,
    SettingsUpdate,
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
        assert types[-1] == "turn.status" and events[-1].state == "done"

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
        assert events[-1].type == "turn.status" and events[-1].state == "failed"
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
        assert events[-1].type == "turn.status" and events[-1].state == "failed"
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
