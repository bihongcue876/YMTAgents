"""MCP 安全体检集成冒烟（v0.0.9）。

按铁律从 `controller.handle(McpScan)` 入口走一次，断言 `mcp.scan.result` 事件；
不起真实网络 / 子进程（stdio 服务器，静态项 + skip 分支）。
"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import McpScan, McpScanResult
from shared.schema import McpServerConfig
from tests.mocks.gateway import MockGateway


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())


def test_mcp_scan_via_controller(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    try:
        modules = ctx.config_store.load("modules")
        modules.mcp.servers = [
            McpServerConfig(id="a", name="演示A", enabled=False, transport="stdio")
        ]
        ctx.config_store.save("modules", modules)
        ctx.mcp_manager.load()

        events: list = []
        original = ctx.controller.emit
        ctx.controller.emit = lambda event: (events.append(event), original(event))[1]

        ctx.controller.handle(McpScan(server_id="a"))

        results = [e for e in events if isinstance(e, McpScanResult)]
        assert len(results) == 1
        report = results[0]
        assert report.server_id == "a"
        assert report.server_name == "演示A"
        assert {f["id"] for f in report.findings} == {"A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"}
        assert report.summary["pass"] == 1  # A3：无明文凭证
        assert report.summary["skip"] == 7  # stdio + 无活连接

        # 服务器缺失 → 走既有错误语义，不崩、不发结果事件
        events.clear()
        ctx.controller.handle(McpScan(server_id="missing"))
        assert not [e for e in events if isinstance(e, McpScanResult)]
    finally:
        ctx.worker.stop()