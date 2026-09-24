"""附加功能总开关端到端（切片 0）：经 controller 入口的真卸载/惰性装配。

真值与路径都走 `controller.handle(FeatureToggle)`；断言状态事件、配置持久、supervisor 同步。
"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import FeatureToggle
from tests.mocks.gateway import MockGateway


def _boot(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=lambda _store: MockGateway())


def _collect(ctx) -> list:
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    return events


def test_default_features_loaded_and_btcm_absent(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    try:
        assert ctx.features.host("shell") is not None
        assert ctx.features.host("skills") is not None
        assert ctx.features.host("mcp") is not None
        assert ctx.features.host("btcm") is None  # 未启用 = 不存在
    finally:
        ctx.worker.stop()


def test_toggle_shell_off_then_on(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        ctx.controller.handle(FeatureToggle(name="shell", enabled=False))
        assert ctx.features.host("shell") is None
        assert ctx.controller.shell_manager is None  # controller 实时属性
        assert ctx.config_store.load("modules").features.shell is False

        state = [e for e in events if e.type == "feature.state"][-1]
        by = {f["name"]: f for f in state.features}
        assert by["shell"]["enabled"] is False and by["shell"]["state"] == "disabled"
        health = [e for e in events if e.type == "health.report"][-1]
        assert health.modules.get("shell") == "disabled"

        # 卸载后 shell.spawn 不得崩（宿主为 None → 静默跳过）
        from shared.envelope import ShellSpawn

        ctx.controller.handle(ShellSpawn())

        # 回切：真重装
        ctx.controller.handle(FeatureToggle(name="shell", enabled=True))
        assert ctx.features.host("shell") is not None
        assert ctx.config_store.load("modules").features.shell is True
    finally:
        ctx.worker.stop()


def test_toggle_skills_off_releases_host(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    try:
        assert ctx.features.host("skills") is not None
        ctx.controller.handle(FeatureToggle(name="skills", enabled=False))
        assert ctx.features.host("skills") is None
        assert not any(s.name.startswith("skill.") for s in ctx.registry.snapshot())
    finally:
        ctx.worker.stop()


def test_toggle_mcp_off_releases_host(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    try:
        ctx.controller.handle(FeatureToggle(name="mcp", enabled=False))
        assert ctx.features.host("mcp") is None
        assert ctx.controller.mcp_manager is None
        assert not any(s.name.startswith("mcp.") for s in ctx.registry.snapshot())
    finally:
        ctx.worker.stop()


def test_unknown_feature_reports_invalid_request(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        ctx.controller.handle(FeatureToggle(name="nope", enabled=True))
        errors = [e for e in events if e.type == "error"]
        assert errors and errors[0].code == "invalid_request"
    finally:
        ctx.worker.stop()


def test_disabled_feature_survives_restart(tmp_path, monkeypatch, qapp):
    """关档落盘后，重启装配同样不再加载（启动即减压）。"""
    ctx = _boot(tmp_path, monkeypatch)
    try:
        ctx.controller.handle(FeatureToggle(name="skills", enabled=False))
    finally:
        ctx.worker.stop()

    ctx2 = _boot(tmp_path, monkeypatch)
    try:
        assert ctx2.features.host("skills") is None
        assert ctx2.features.enabled("skills") is False
    finally:
        ctx2.worker.stop()