"""启动装配与请求分派集成测试（core 侧，无 GUI）。"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import NewSession, ResumeSession, SendMessage, SwitchModel
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
