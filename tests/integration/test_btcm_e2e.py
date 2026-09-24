"""副思考链端到端（切片 0–4）：门控、工具回路、自动档策略、页面运行通道。

全部经 `controller.handle(Request)` 入口；替身网关消费被测数据（按 system 提示词分工）。
"""

from __future__ import annotations

import json

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import (
    BtcmRun,
    BtcmUpdate,
    FeatureToggle,
    NewSession,
    SendMessage,
)
from tests.mocks.btcm_gateway import BtcmMockGateway


def _boot(tmp_path, monkeypatch, gateway=None):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    gateway = gateway or BtcmMockGateway(verdicts=["pass"], slots={"main": "mock-model"})
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda _store: gateway)
    return ctx, gateway


def _collect(ctx) -> list:
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    return events


def _enable(ctx, trigger: str | None = None) -> None:
    ctx.controller.handle(FeatureToggle(name="btcm", enabled=True))
    if trigger is not None:
        ctx.controller.handle(BtcmUpdate(trigger=trigger))


def _tool_names(ctx) -> set[str]:
    return {s.name for s in ctx.registry.snapshot()}


def test_btcm_is_unloaded_by_default(tmp_path, monkeypatch, qapp):
    """默认关 = 真卸载：不注册工具，且请求按未启用收口。"""
    ctx, _gateway = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        assert ctx.features.host("btcm") is None
        assert "btcm.think" not in _tool_names(ctx)
        state = [e for e in events if e.type == "feature.state"][-1] if events else None
        assert state is None  # 未 push_initial_state 前无事件

        ctx.controller.push_initial_state()
        by = {f["name"]: f for f in [e for e in events if e.type == "feature.state"][-1].features}
        assert by["btcm"] == {
            "name": "btcm",
            "enabled": False,
            "state": "disabled",
            "available": True,
        }
        assert by["dpim"] == {
            "name": "dpim",
            "enabled": False,
            "state": "disabled",
            "available": True,
        }
        assert [e for e in events if e.type == "btcm.state"][-1].ready is False

        ctx.controller.handle(BtcmRun(question="q"))
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[-1].code == "invalid_request"
    finally:
        ctx.worker.stop()


def test_toggle_on_registers_tool_and_reports_state(tmp_path, monkeypatch, qapp):
    ctx, _gateway = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        _enable(ctx, trigger="manual")
        assert ctx.features.host("btcm") is not None
        assert "btcm.think" in _tool_names(ctx)
        assert ctx.config_store.load("modules").features.btcm is True

        state = [e for e in events if e.type == "btcm.state"][-1]
        assert (state.trigger, state.slot, state.ready) == ("manual", "thinking", True)
        health = [e for e in events if e.type == "health.report"][-1]
        assert health.modules["btcm"] == "ready"
    finally:
        ctx.worker.stop()


def test_toggle_off_unloads_again(tmp_path, monkeypatch, qapp):
    ctx, _gateway = _boot(tmp_path, monkeypatch)
    try:
        _enable(ctx, trigger="manual")
        ctx.controller.handle(FeatureToggle(name="btcm", enabled=False))
        assert ctx.features.host("btcm") is None
        assert "btcm.think" not in _tool_names(ctx)
        assert ctx.config_store.load("modules").features.btcm is False
    finally:
        ctx.worker.stop()


def test_btcm_update_persists_and_pushes_state(tmp_path, monkeypatch, qapp):
    ctx, _gateway = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        _enable(ctx)
        ctx.controller.handle(BtcmUpdate(trigger="auto", slot="main"))
        saved = ctx.config_store.load("modules").btcm
        assert (saved.trigger, saved.slot) == ("auto", "main")
        state = [e for e in events if e.type == "btcm.state"][-1]
        assert (state.trigger, state.slot) == ("auto", "main")
    finally:
        ctx.worker.stop()


def test_btcm_run_uses_same_tool_path(tmp_path, monkeypatch, qapp):
    """页面「运行一次」→ 与 `btcm.think` 同一条工具路径（tool.call/tool.result + 思考流）。"""
    ctx, gateway = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        _enable(ctx, trigger="manual")
        ctx.controller.handle(NewSession())
        ctx.controller.handle(BtcmRun(question="需要深思的问题", effort="standard", mode="auto"))

        call = [e for e in events if e.type == "tool.call"][-1]
        assert call.name == "btcm.think"
        assert call.permission == "safe"
        result = [e for e in events if e.type == "tool.result"][-1]
        assert result.ok is True
        payload = json.loads(result.output)
        assert payload["verdict"] == "pass"
        assert result.usage and result.usage["total_tokens"] == 30

        assert [e for e in events if e.type == "think.delta"]
        assert [e for e in events if e.type == "think.iteration"]
        assert call.call_id.startswith("btcm-page-")
        # 引擎用的是宿主网关（本对话模型优先）
        assert gateway.calls[0]["model_id"] == "mock-model"
    finally:
        ctx.worker.stop()


def test_auto_trigger_mounts_policy_line_only_in_auto(tmp_path, monkeypatch, qapp):
    """自动档才挂环境声明策略行（明示、可审计；手动档不挂）。"""
    ctx, gateway = _boot(tmp_path, monkeypatch)
    try:
        _enable(ctx, trigger="auto")
        ctx.controller.handle(NewSession())
        ctx.controller.handle(SendMessage(text="请回答"))
        system = gateway.calls[0]["messages"][0]["content"]
        assert "附加策略" in system and "btcm.think" in system

        gateway.calls.clear()
        ctx.controller.handle(BtcmUpdate(trigger="manual"))
        ctx.controller.handle(SendMessage(text="再回答"))
        system = gateway.calls[0]["messages"][0]["content"]
        assert "附加策略" not in system
    finally:
        ctx.worker.stop()


def test_disabled_feature_has_no_policy_line_and_no_tool(tmp_path, monkeypatch, qapp):
    ctx, gateway = _boot(tmp_path, monkeypatch)
    try:
        ctx.controller.handle(FeatureToggle(name="btcm", enabled=True))
        ctx.controller.handle(BtcmUpdate(trigger="auto"))
        ctx.controller.handle(FeatureToggle(name="btcm", enabled=False))
        ctx.controller.handle(NewSession())
        ctx.controller.handle(SendMessage(text="请回答"))
        system = gateway.calls[0]["messages"][0]["content"]
        assert "附加策略" not in system
        assert "btcm.think" not in _tool_names(ctx)
    finally:
        ctx.worker.stop()


def test_feature_toggle_survives_restart(tmp_path, monkeypatch, qapp):
    """开档落盘后重启仍装配（与 default 关档互为反证）。"""
    ctx, _gateway = _boot(tmp_path, monkeypatch)
    try:
        _enable(ctx, trigger="manual")
    finally:
        ctx.worker.stop()

    ctx2, _gateway2 = _boot(tmp_path, monkeypatch)
    try:
        assert ctx2.features.enabled("btcm") is True
        assert "btcm.think" in _tool_names(ctx2)
    finally:
        ctx2.worker.stop()
