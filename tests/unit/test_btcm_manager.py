"""副思考链宿主单元测试（切片 2）：工具注册/卸载、子选项、模型路由、错误映射、计量回填。"""

from __future__ import annotations

import json

from core.modules.btcm.manager import TOOL_THINK, TOOL_TIMEOUT_MS, BtcmManager
from core.registry.executor import ToolContext
from core.registry.registry import Registry
from core.store.config_store import ConfigStore
from shared.enums import Permission
from tests.mocks.btcm_gateway import BtcmMockGateway


def _manager(tmp_path, gateway=None, **kwargs):
    registry = Registry()
    store = ConfigStore(tmp_path)
    audits: list[dict] = []
    manager = BtcmManager(
        registry,
        store,
        gateway or BtcmMockGateway(verdicts=["pass"], slots={"main": "mock-model"}),
        audit=lambda ev, **kw: audits.append({"ev": ev, **kw}),
        **kwargs,
    )
    return manager, registry, store, audits


def _spec(registry: Registry):
    return next((s for s in registry.snapshot() if s.name == TOOL_THINK), None)


def test_activate_registers_readonly_tool(tmp_path):
    manager, registry, _store, _audits = _manager(tmp_path)
    manager.activate()
    spec = _spec(registry)
    assert spec is not None
    assert spec.permission == Permission.SAFE  # 只读：免关卡
    assert spec.timeout_ms == TOOL_TIMEOUT_MS
    assert spec.input_schema["required"] == ["question"]
    assert manager.host_state() == "ready"


def test_deactivate_unregisters_tool(tmp_path):
    manager, registry, _store, _audits = _manager(tmp_path)
    manager.activate()
    manager.deactivate()
    assert _spec(registry) is None
    assert manager.host_state() == "disabled"


def test_legacy_off_trigger_is_normalized_to_manual(tmp_path):
    manager, _registry, store, _audits = _manager(tmp_path)
    modules = store.load("modules")
    modules.btcm.trigger = "off"  # 存量值（宿主级启停已改由 features.btcm 管）
    store.save("modules", modules)
    manager.activate()
    assert manager.state_payload()["trigger"] == "manual"
    assert manager.state_payload()["ready"] is True


def test_update_persists_trigger_and_slot_and_audits(tmp_path):
    manager, _registry, store, audits = _manager(tmp_path)
    manager.activate()
    manager.update(trigger="auto", slot="main")
    saved = store.load("modules").btcm
    assert (saved.trigger, saved.slot) == ("auto", "main")
    assert {"ev": "btcm.update", "trigger": "auto", "slot": "main"} in audits
    # 非法值不落盘（白名单）
    manager.update(trigger="nonsense", slot="nope")
    saved = store.load("modules").btcm
    assert (saved.trigger, saved.slot) == ("auto", "main")


def test_empty_question_is_invalid_args(tmp_path):
    manager, _registry, _store, _audits = _manager(tmp_path)
    manager.activate()
    result = manager._handle({"question": "   "}, ToolContext(session_id="s"))
    assert result.ok is False
    assert result.error["code"] == "tool_invalid_args"


def test_unbound_model_reports_tool_unavailable(tmp_path):
    gateway = BtcmMockGateway(slots={})
    manager, _registry, _store, _audits = _manager(tmp_path, gateway=gateway)
    manager.activate()
    result = manager._handle({"question": "q"}, ToolContext(session_id="s"))
    assert result.ok is False
    assert result.error["code"] == "tool_unavailable"
    assert "模型" in result.error["message"]


def test_handle_returns_json_and_usage(tmp_path):
    gateway = BtcmMockGateway(verdicts=["pass"], slots={"main": "mock-model"})
    manager, _registry, _store, audits = _manager(tmp_path, gateway=gateway)
    manager.activate()
    ctx = ToolContext(session_id="s", call_id="call-1")
    result = manager._handle({"question": "需要深思的问题"}, ctx)
    assert result.ok is True
    payload = json.loads(result.output)
    assert payload["verdict"] == "pass"
    assert payload["termination_reason"] == "validation_passed"
    assert result.usage["total_tokens"] == 30  # 三次 Agent 调用累加（替身每次 10）
    # 审计只记口径，不记问题原文
    think_audits = [a for a in audits if a["ev"] == "btcm.think"]
    assert think_audits and "question" not in think_audits[0]


def test_invalid_effort_and_mode_fall_back(tmp_path):
    gateway = BtcmMockGateway(verdicts=["pass"], slots={"main": "mock-model"})
    manager, _registry, _store, _audits = _manager(tmp_path, gateway=gateway)
    manager.activate()
    result = manager._handle(
        {"question": "q", "effort": "ultra", "mode": "weird"}, ToolContext(session_id="s")
    )
    assert result.ok is True
    assert gateway.agent_calls == ["creative", "validator", "meta"]  # 归一为 standard/auto


def test_session_model_takes_precedence(tmp_path):
    gateway = BtcmMockGateway(verdicts=["pass"], slots={"main": "m-main", "thinking": "m-think"})
    manager, _registry, store, _audits = _manager(
        tmp_path, gateway=gateway, resolve_session_model=lambda _sid: "m-session"
    )
    modules = store.load("modules")
    modules.btcm.slot = "thinking"
    store.save("modules", modules)
    manager.activate()
    manager._handle({"question": "q"}, ToolContext(session_id="s"))
    assert gateway.calls[0]["model_id"] == "m-session"
    # 会话无模型时回退到指定槽位
    gateway.calls.clear()
    manager._handle({"question": "q"}, ToolContext(session_id=""))
    assert gateway.calls[0]["model_id"] == "m-think"


def test_engine_failure_maps_to_backend_error(tmp_path):
    gateway = BtcmMockGateway(slots={"main": "mock-model"}, fail_agents=("creative",))
    manager, _registry, _store, _audits = _manager(tmp_path, gateway=gateway)
    manager.activate()
    result = manager._handle({"question": "q"}, ToolContext(session_id="s"))
    assert result.ok is False
    assert result.error["code"] == "tool_backend_error"