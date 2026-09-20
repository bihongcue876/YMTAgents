"""core.mcp.manager 单元测试（v0.0.3 rev41）。

覆盖：命名空间 sanitize、注册与权限覆盖、host_state 聚合、工具条目、注销回收。
不起真实子进程（直测 `_register_tools` 与配置生命周期）。
"""

from __future__ import annotations

from shared.schema import McpServerConfig

from core.mcp.manager import McpManager, sanitize_component
from core.mcp.transport import McpTool
from core.registry.registry import Registry
from core.store.config_store import ConfigStore


class _NoSecrets:
    def get(self, name):
        return None


class _FakeClient:
    def call_tool(self, name, args):
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


def test_sanitize_component():
    assert sanitize_component("My-Tool.X") == "my_tool_x"
    assert sanitize_component("") == "tool"


def test_register_tools_namespace_and_permission(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    registry = Registry()
    mgr = McpManager(registry, store, _NoSecrets())
    cfg = McpServerConfig(
        id="Demo Server", name="演示", transport="stdio",
        tool_permissions={"Echo-Tool": "safe"},
    )
    mgr._register_tools(
        cfg,
        _FakeClient(),
        [McpTool(name="Echo-Tool", description="回声", input_schema={"type": "object"})],
    )
    names = [s.name for s in registry.snapshot()]
    assert names == ["mcp.demo_server.echo_tool"]
    spec = registry.snapshot()[0]
    assert spec.permission.value == "safe"  # 逐工具显式覆盖
    assert spec.title == "演示 · Echo-Tool"

    entries = mgr.tool_entries()
    assert entries[0]["original"] == "Echo-Tool"
    assert entries[0]["server"] == "Demo Server"

    # 注销回收
    calls = mgr._owned["Demo Server"]
    mgr._unregister_server("Demo Server")
    assert registry.snapshot() == []
    assert calls == ["mcp.demo_server.echo_tool"]
    assert mgr.tool_entries() == []


def test_default_permission_is_confirm(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    registry = Registry()
    mgr = McpManager(registry, store, _NoSecrets())
    cfg = McpServerConfig(id="s1", name="S1", transport="stdio")
    mgr._register_tools(cfg, _FakeClient(), [McpTool(name="danger")])
    assert registry.snapshot()[0].permission.value == "confirm"


def test_host_state_and_lifecycle(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    modules = store.load("modules")
    modules.mcp.servers = [
        McpServerConfig(id="a", name="A", enabled=False, transport="stdio"),
        McpServerConfig(id="b", name="B", enabled=False, transport="stdio"),
    ]
    store.save("modules", modules)

    registry = Registry()
    mgr = McpManager(registry, store, _NoSecrets())
    mgr.load()
    assert mgr.host_state() == "disabled"  # 无 enabled server
    status = mgr.list_status()
    assert {s["id"] for s in status} == {"a", "b"}
    assert all(s["state"] == "stopped" for s in status)

    # enabled 但连接失败 => error 聚合（命令不存在）
    mgr.set_enabled("a", True)
    assert mgr.host_state() == "error"
    assert mgr.list_status()[0]["error"]

    # 关闭后回到 disabled
    mgr.set_enabled("a", False)
    assert mgr.host_state() == "disabled"


def test_security_scan_reserved_is_noop(tmp_path):
    """安全检测为预留扩展点：默认 NullScanner 恒返回空，不影响既有行为。"""
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    modules = store.load("modules")
    modules.mcp.servers = [McpServerConfig(id="a", name="A", transport="stdio")]
    store.save("modules", modules)

    mgr = McpManager(Registry(), store, _NoSecrets())
    mgr.load()
    assert mgr.scan("a") == []
    assert mgr.scan("missing") == []