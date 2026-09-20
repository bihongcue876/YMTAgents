"""验收集成测试 A1–A12（spec §4 / rev2 §4）。"""

from __future__ import annotations

import json

from app import bootstrap as bootstrap_mod
from app import paths
from core.gateway.provider import ModelGateway
from core.gateway.whitelist import Whitelist
from shared.envelope import (
    ArchiveSession,
    DeleteSession,
    ModelSpec,
    NewSession,
    ProviderSpec,
    ProviderUpsert,
    ResumeSession,
    SendMessage,
    SwitchModel,
    TestConnection,
    UnarchiveSession,
)
from tests.mocks.gateway import MockGateway
from tests.mocks.secrets import FakeVault


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class _Usage:
    def __init__(self):
        self.prompt_tokens = 1
        self.completion_tokens = 1
        self.total_tokens = 2


class _Delta:
    content = "ok"


class _Choice:
    delta = _Delta()


class _Completions:
    def create(self, **kwargs):
        if kwargs.get("stream"):
            class _C:
                choices = [_Choice()]
                usage = _Usage()
            return iter([_C()])
        return object()


class _Chat:
    completions = _Completions()


class _Client:
    chat = _Chat()


def _boot(tmp_path, monkeypatch, gateway_factory):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=gateway_factory)


def _collect(ctx):
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    return events


def _real_gateway_factory(secrets, chunks_client=True):
    return lambda store: ModelGateway(
        store,
        secrets=secrets,
        client_factory=lambda base_url, api_key: _Client(),
    )


def _spec(model_id="m1", base_url="https://api.test.com/v1"):
    return ProviderSpec(id="prv_1", name="P", base_url=base_url, models=[ModelSpec(id=model_id)])


# ---------------------------------------------------------------------------
# A1 首启零配置
# ---------------------------------------------------------------------------
def test_a1_empty_state(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, _real_gateway_factory(FakeVault()))
    events = _collect(ctx)
    try:
        ctx.controller.push_initial_state()
        provider_list = [e for e in events if e.type == "provider.list"][0]
        session_index = [e for e in events if e.type == "session.index"][0]
        assert provider_list.providers == []
        assert session_index.sessions == []
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A2 添加供应商（密钥入加密库 / models.json / 白名单）
# ---------------------------------------------------------------------------
def test_a2_upsert_provider(tmp_path, monkeypatch, qapp):
    vault = FakeVault()
    ctx = _boot(tmp_path, monkeypatch, _real_gateway_factory(vault))
    events = _collect(ctx)
    try:
        ctx.controller.handle(ProviderUpsert(provider=_spec(), api_key="sk-abc"))
        provider_list = [e for e in events if e.type == "provider.list"][-1]
        assert provider_list.providers[0].key_status == "stored"
        assert vault.data["api_key/prv_1"] == "sk-abc"
        models = json.loads((ctx.root / "config" / "models.json").read_text(encoding="utf-8"))
        assert models["providers"][0]["id"] == "prv_1"
        settings = json.loads((ctx.root / "config" / "settings.json").read_text(encoding="utf-8"))
        assert "api.test.com" in settings["network"]["whitelist"]
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A3 测试连接
# ---------------------------------------------------------------------------
def test_a3_test_connection(tmp_path, monkeypatch, qapp):
    vault = FakeVault()
    ctx = _boot(tmp_path, monkeypatch, _real_gateway_factory(vault))
    events = _collect(ctx)
    try:
        ctx.controller.handle(ProviderUpsert(provider=_spec(), api_key="sk"))
        events.clear()
        ctx.controller.handle(TestConnection(provider_id="prv_1", model_id="m1"))
        result = [e for e in events if e.type == "provider.test.result"][0]
        assert result.ok is True
        assert result.latency_ms is not None
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A4 发送消息
# ---------------------------------------------------------------------------
def test_a4_send_and_log(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, lambda store: MockGateway())
    events = _collect(ctx)
    try:
        ctx.controller.handle(NewSession())
        sid = ctx.controller.current_session_id
        events.clear()
        ctx.controller.handle(SendMessage(text="hi"))
        types = [e.type for e in events]
        assert "msg.assistant.delta" in types
        statuses = [e for e in events if e.type == "turn.status"]
        assert statuses and statuses[-1].state == "done"
        logged = [e["type"] for e in ctx.session_store.replay(sid)]
        for expected in ("session.start", "msg.user", "msg.assistant.delta", "msg.assistant.final", "ctx.usage"):
            assert expected in logged
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A5 中断
# ---------------------------------------------------------------------------
def test_a5_interrupt(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, lambda store: MockGateway(cancel_after=0))
    events = _collect(ctx)
    try:
        ctx.controller.handle(NewSession())
        sid = ctx.controller.current_session_id
        events.clear()
        ctx.controller.handle(SendMessage(text="hi"))
        final = [e for e in events if e.type == "msg.assistant.final"][-1]
        assert final.interrupted is True
        assert any(e["type"] == "interrupt" for e in ctx.session_store.replay(sid))
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A6 会话列表操作
# ---------------------------------------------------------------------------
def test_a6_session_ops(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, lambda store: MockGateway())
    try:
        ctx.controller.handle(NewSession(title="A"))
        sid = ctx.controller.current_session_id
        assert len(ctx.session_store.list()) == 1
        ctx.controller.handle(ArchiveSession(session_id=sid))
        assert ctx.session_store.list() == []
        assert len(ctx.session_store.list(include_archived=True)) == 1
        ctx.controller.handle(UnarchiveSession(session_id=sid))
        assert len(ctx.session_store.list()) == 1
        ctx.controller.handle(DeleteSession(session_id=sid))
        assert ctx.session_store.list(include_archived=True) == []
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A7 会话切换回放
# ---------------------------------------------------------------------------
def test_a7_replay(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, lambda store: MockGateway())
    events = _collect(ctx)
    try:
        ctx.controller.handle(NewSession())
        sid = ctx.controller.current_session_id
        ctx.controller.handle(SendMessage(text="hi"))
        events.clear()
        ctx.controller.handle(ResumeSession(session_id=sid))
        replay = [e for e in events if e.type == "session.events"][0]
        assert any(ev["type"] == "msg.user" for ev in replay.events)
        assert any(ev["type"] == "msg.assistant.final" for ev in replay.events)
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A8 静默超时
# ---------------------------------------------------------------------------
def test_a8_timeout(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, lambda store: MockGateway(timeout=True))
    events = _collect(ctx)
    try:
        ctx.controller.handle(NewSession())
        events.clear()
        ctx.controller.handle(SendMessage(text="hi"))
        assert any(e.type == "error" for e in events)
        statuses = [e for e in events if e.type == "turn.status"]
        assert statuses and statuses[-1].state == "failed"
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A9 白名单拦截
# ---------------------------------------------------------------------------
def test_a9_whitelist_blocked(tmp_path, monkeypatch, qapp):
    vault = FakeVault()

    def factory(store):
        gateway = ModelGateway(store, secrets=vault, client_factory=lambda b, k: _Client())
        return gateway

    ctx = _boot(tmp_path, monkeypatch, factory)
    events = _collect(ctx)
    try:
        ctx.controller.handle(ProviderUpsert(provider=_spec(), api_key="sk"))
        ctx.gateway.whitelist = Whitelist([])  # 模拟白名单外
        ctx.gateway.set_slot("main", "m1")
        ctx.controller.handle(NewSession())
        events.clear()
        ctx.controller.handle(SendMessage(text="hi"))
        error = [e for e in events if e.type == "error"][0]
        assert error.code == "whitelist_blocked"
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A10 密钥缺失
# ---------------------------------------------------------------------------
def test_a10_key_missing(tmp_path, monkeypatch, qapp):
    vault = FakeVault()
    ctx = _boot(tmp_path, monkeypatch, _real_gateway_factory(vault))
    events = _collect(ctx)
    try:
        ctx.controller.handle(ProviderUpsert(provider=_spec(), api_key=None))
        ctx.gateway.set_slot("main", "m1")
        ctx.controller.handle(NewSession())
        events.clear()
        ctx.controller.handle(SendMessage(text="hi"))
        error = [e for e in events if e.type == "error"][0]
        assert error.code == "key_missing"
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A11 模型切换
# ---------------------------------------------------------------------------
def test_a11_model_switch(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, lambda store: MockGateway())
    try:
        ctx.controller.handle(NewSession())
        sid = ctx.controller.current_session_id
        ctx.controller.handle(SwitchModel(slot="main", model_id="mock-model"))
        assert any(e["type"] == "model.switch" for e in ctx.session_store.replay(sid))
        assert ctx.session_store.get_meta(sid).main_model == "mock-model"
    finally:
        ctx.worker.stop()


# ---------------------------------------------------------------------------
# A12 断电恢复（末行截断）
# ---------------------------------------------------------------------------
def test_a12_truncated_recovery(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch, lambda store: MockGateway())
    try:
        ctx.controller.handle(NewSession())
        sid = ctx.controller.current_session_id
        path = ctx.root / "sessions" / sid / "events.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"v":1,"seq":999,"type":"broken"\n')  # 半行
        events = ctx.session_store.replay(sid)
        assert all(e.get("seq") != 999 for e in events)
        audit = ctx.root / "logs" / "audit.jsonl"
        assert audit.exists()
        assert "event_stream_truncated" in audit.read_text(encoding="utf-8")
    finally:
        ctx.worker.stop()
