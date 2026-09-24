"""附加功能生命周期（切片 0）单元测试。

覆盖六条硬指标中最能在单元层判定的部分：不 import（惰性工厂）、不注册（卸载即注销）、
释放引用（host 置空）、状态可见（states）、可回切（再开即重装）。
"""

from __future__ import annotations

import sys

import pytest

from core.modules.feature import IFeatureHost
from core.modules.manager import FeatureManager
from core.registry.registry import Registry
from core.registry.toolspec import ToolSpec
from core.store.config_store import ConfigStore


class _Host(IFeatureHost):
    def __init__(self, registry: Registry | None = None, tool: str = "probe.tool") -> None:
        self._registry = registry
        self._tool = tool
        self.activated = 0
        self.deactivated = 0

    def activate(self) -> None:
        self.activated += 1
        if self._registry is not None:
            self._registry.register(ToolSpec(name=self._tool), lambda _a, _c: None)

    def deactivate(self) -> None:
        self.deactivated += 1
        if self._registry is not None:
            self._registry.unregister(self._tool)

    def host_state(self) -> str:
        return "ready"


def _names(registry: Registry) -> set[str]:
    return {s.name for s in registry.snapshot()}


def test_disabled_feature_is_not_built(tmp_path):
    """默认关的附加功能：`load()` 不构造宿主，工厂一次都不调用（不 import）。"""
    mgr = FeatureManager(ConfigStore(tmp_path))
    called: list[str] = []
    mgr.register("btcm", lambda: (called.append("btcm"), _Host())[1])
    mgr.load()
    assert called == []
    assert mgr.host("btcm") is None
    assert mgr.enabled("btcm") is False


def test_factory_import_is_lazy(tmp_path):
    """关档时工厂体内的 import 不发生；开启后才进入 `sys.modules`。"""
    probe = "tests.mocks.feature_probe"
    sys.modules.pop(probe, None)
    mgr = FeatureManager(ConfigStore(tmp_path))

    def factory():
        import importlib

        importlib.import_module(probe)
        return _Host()

    mgr.register("btcm", factory)
    mgr.load()
    assert probe not in sys.modules
    mgr.toggle("btcm", True)
    assert probe in sys.modules


def test_toggle_on_activates_and_persists(tmp_path):
    store = ConfigStore(tmp_path)
    mgr = FeatureManager(store)
    mgr.register("btcm", lambda: _Host())
    mgr.toggle("btcm", True)
    assert mgr.host("btcm") is not None
    assert store.load("modules").features.btcm is True
    assert FeatureManager(store).enabled("btcm") is True  # 同一文件：持久生效


def test_toggle_off_releases_and_unregisters(tmp_path):
    registry = Registry()
    mgr = FeatureManager(ConfigStore(tmp_path))
    mgr.register("btcm", lambda: _Host(registry, tool="btcm.think"))
    mgr.toggle("btcm", True)
    assert "btcm.think" in _names(registry)
    host = mgr.host("btcm")
    mgr.toggle("btcm", False)
    assert mgr.host("btcm") is None  # 释放引用
    assert "btcm.think" not in _names(registry)  # 真注销
    assert host is not None and host.deactivated == 1


def test_reactivate_after_off(tmp_path):
    registry = Registry()
    mgr = FeatureManager(ConfigStore(tmp_path))
    mgr.register("btcm", lambda: _Host(registry, tool="btcm.think"))
    mgr.toggle("btcm", True)
    mgr.toggle("btcm", False)
    mgr.toggle("btcm", True)
    assert mgr.host("btcm") is not None
    assert "btcm.think" in _names(registry)


def test_states_report_disabled_then_ready(tmp_path):
    store = ConfigStore(tmp_path)
    mgr = FeatureManager(store)
    mgr.register("btcm", lambda: _Host())
    states = {item["name"]: item for item in mgr.states()}
    assert states["btcm"] == {
        "name": "btcm",
        "enabled": False,
        "state": "disabled",
        "available": True,
    }
    assert states["dpim"] == {
        "name": "dpim",
        "enabled": False,
        "state": "unavailable",
        "available": False,
    }
    with pytest.raises(ValueError):
        mgr.toggle("dpim", True)
    assert store.load("modules").features.dpim is False
    mgr.toggle("btcm", True)
    states = {item["name"]: item for item in mgr.states()}
    assert states["btcm"] == {
        "name": "btcm",
        "enabled": True,
        "state": "ready",
        "available": True,
    }


def test_unknown_feature_raises(tmp_path):
    mgr = FeatureManager(ConfigStore(tmp_path))
    with pytest.raises(ValueError):
        mgr.toggle("nope", True)


def test_load_activates_enabled_features(tmp_path):
    """`load()` 只装配已启用者：保真默认（mcp/shell/skills 开，btcm/dpim 关）。"""
    mgr = FeatureManager(ConfigStore(tmp_path))
    mgr.register("shell", lambda: _Host(tool="shell.exec"))
    mgr.register("btcm", lambda: _Host(tool="btcm.think"))
    mgr.load()
    assert mgr.host("shell") is not None
    assert mgr.host("btcm") is None


class _BrokenHost(IFeatureHost):
    def activate(self) -> None:
        raise RuntimeError("装配失败")

    def deactivate(self) -> None:  # pragma: no cover - 装配失败后不会被调用
        return None

    def host_state(self) -> str:
        raise RuntimeError("状态查询失败")


def test_toggle_on_failure_reverts_truth(tmp_path):
    """开档装配失败：**真值回退**（不留「配置为开、宿主不存在」的错位态）。"""
    store = ConfigStore(tmp_path)
    audits: list[dict] = []
    mgr = FeatureManager(store, audit=lambda ev, **kw: audits.append({"ev": ev, **kw}))
    mgr.register("btcm", _BrokenHost)
    assert mgr.toggle("btcm", True) is False
    assert mgr.host("btcm") is None
    assert store.load("modules").features.btcm is False
    state = next(item for item in mgr.states() if item["name"] == "btcm")
    assert state == {
        "name": "btcm",
        "enabled": False,
        "state": "disabled",
        "available": True,
    }
    assert {"ev": "feature.activate", "name": "btcm", "ok": False} in audits


def test_activation_failure_does_not_block_load(tmp_path):
    """单个功能装配失败不得阻断启动：其余功能照常装配。"""
    store = ConfigStore(tmp_path)
    mgr = FeatureManager(store)
    mgr.register("shell", _BrokenHost)
    mgr.register("skills", lambda: _Host())
    mgr.load()
    assert mgr.host("shell") is None
    assert mgr.host("skills") is not None


def test_host_state_error_is_surfaced(tmp_path):
    """状态查询异常归 `error`，不外溢到调用方。"""
    mgr = FeatureManager(ConfigStore(tmp_path))
    mgr.register("btcm", _BrokenHost)
    # 直接登记宿主，绕过装配（模拟「已装配但状态查询坏掉」）。
    mgr._hosts["btcm"] = _BrokenHost()  # 刻意构造「已装配但状态查询坏掉」的宿主
    state = next(item for item in mgr.states() if item["name"] == "btcm")
    assert state == {
        "name": "btcm",
        "enabled": False,
        "state": "error",
        "available": True,
    }


def test_state_payload_defaults_to_empty(tmp_path):
    """无子选项的宿主默认返回空表（界面据此判定「无子选项」）。"""
    assert _Host().state_payload() == {}
