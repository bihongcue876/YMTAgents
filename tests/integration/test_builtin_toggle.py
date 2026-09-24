"""v0.0.11（D-1）：内置工具开关落在 plugins.json -> builtin（契约 68/47 的一部分）。"""

from __future__ import annotations

import json

from shared.envelope import BuiltinToolState, BuiltinToolToggle

from tests.integration.test_e2e import _boot, _real_gateway_factory
from tests.mocks.secrets import FakeVault


def test_builtin_toggle_persists_and_emits_state(tmp_path, monkeypatch):
    ctx = _boot(tmp_path, monkeypatch, _real_gateway_factory(FakeVault()))
    try:
        fm = ctx.controller.files_manager
        assert fm is not None
        assert [item["name"] for item in fm.state()][:2] == ["file.read", "file.glob"]

        events: list = []
        ctx.bridge.event_received.connect(events.append)
        ctx.controller.handle(BuiltinToolToggle(name="file.grep", enabled=False))
        states = [e for e in events if isinstance(e, BuiltinToolState)]
        assert states and any(
            item["name"] == "file.grep" and item["enabled"] is False
            for item in states[-1].items
        )
        assert "file.grep" not in {
            spec.name for spec in ctx.controller.registry.snapshot()
        }

        data_path = ctx.root / "config" / "plugins.json"
        assert data_path.exists(), "开关必须落盘 plugins.json -> builtin"
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        assert raw["builtin"]["file.grep"]["enabled"] is False
    finally:
        pass  # AppContext 无 close：线程由 conftest._reap_core_workers 回收


def test_builtin_toggle_unknown_name_reports_tool_unknown(tmp_path, monkeypatch):
    ctx = _boot(tmp_path, monkeypatch, _real_gateway_factory(FakeVault()))
    try:
        events: list = []
        ctx.bridge.event_received.connect(events.append)
        ctx.controller.handle(BuiltinToolToggle(name="nope.tool", enabled=False))
        errors = [
            getattr(e, "code", None)
            for e in events
            if getattr(e, "code", None) is not None
        ]
        assert errors == ["tool_unknown"], errors
    finally:
        pass  # AppContext 无 close：线程由 conftest._reap_core_workers 回收
