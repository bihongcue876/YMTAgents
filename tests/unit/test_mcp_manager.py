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


def test_security_scan_reports_findings(tmp_path):
    """v0.0.9：默认检测器为 McpScanner。

    stdio 服务器未连接时：A1/A2 记 skip（不涉及网络），A3 pass（无明文凭证），
    其余主动/工具项无活连接记 skip；服务器不存在返回 None/空。
    """
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    modules = store.load("modules")
    modules.mcp.servers = [McpServerConfig(id="a", name="A", transport="stdio")]
    store.save("modules", modules)

    mgr = McpManager(Registry(), store, _NoSecrets())
    mgr.load()

    findings = mgr.scan("a")
    assert [f.id for f in findings] == ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
    by_id = {f.id: f.status for f in findings}
    assert by_id["A1"] == "skip" and by_id["A2"] == "skip"
    assert by_id["A3"] == "pass"
    assert all(by_id[c] == "skip" for c in ("A4", "B1", "B2", "B3", "B4"))
    assert mgr.scan("missing") == []

    report = mgr.scan_report("a")
    assert report is not None
    assert report["server_name"] == "A"
    assert report["summary"]["pass"] == 1 and report["summary"]["skip"] == 7
    assert mgr.scan_report("missing") is None