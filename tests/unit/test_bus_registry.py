"""core.bus / core.modules / core.registry 单元测试。"""

from __future__ import annotations

import pytest

from core.bus.bridge import BusBridge
from core.modules.supervisor import ModuleSupervisor
from core.registry.registry import Registry
from core.registry.toolspec import ToolSpec


def test_bridge_submit_valid(qapp):
    bridge = BusBridge()
    received: list = []
    bridge.event_received.connect(received.append)
    bridge.submit({"type": "msg.user", "text": "hi"})
    request = bridge.get(timeout=0.2)
    assert request is not None and request.type == "msg.user"


def test_bridge_invalid_request(qapp):
    bridge = BusBridge()
    received: list = []
    bridge.event_received.connect(received.append)
    bridge.submit({"type": "unknown.type"})
    assert bridge.get(timeout=0.2) is None
    assert received and received[0].type == "error"
    assert received[0].code == "invalid_request"


def test_supervisor_all_disabled():
    states = ModuleSupervisor().get_states()
    assert states == {
        "dpim": "disabled",
        "btcm": "disabled",
        "mcp": "disabled",
        "shell": "disabled",
    }


def test_registry_contract():
    registry = Registry()
    assert registry.snapshot() == []

    spec = ToolSpec(name="demo.run", permission="safe")
    registry.register(spec, lambda args, ctx: None)
    assert [s.name for s in registry.snapshot()] == ["demo.run"]

    with pytest.raises(ValueError):
        registry.register(spec, lambda args, ctx: None)

    result = registry.execute("missing.tool", {})
    assert result.ok is False
    assert result.error["code"] == "unavailable"
