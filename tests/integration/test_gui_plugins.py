"""GUI 插件页与关卡卡片冒烟测试（v0.0.3 rev45，offscreen）。

仅验证新组分可构造、可渲染、配置解析与裁决结果正确（不做真实网络/子进程）。
"""

from __future__ import annotations

from types import SimpleNamespace

from gui.pages.plugins import GateDialog, PluginsPage, ServerDialog


def _server(**over):
    base = {
        "id": "demo", "name": "演示", "enabled": True, "transport": "stdio",
        "state": "ready", "tools": ["mcp.demo.echo"], "error": None,
    }
    base.update(over)
    return base


def test_plugins_page_renders_and_updates(qapp):
    page = PluginsPage()
    page.update_servers([_server()])
    page.update_tools([{"name": "mcp.demo.echo", "original": "echo", "permission": "confirm", "server": "demo"}])
    # 卡片已渲染（body 内至少一个 widget）
    assert page._body.count() >= 1

    # 单 server 状态增量更新
    page.update_status("demo", "error", [], "连接失败")
    assert page._servers[0]["state"] == "error"
    assert page._servers[0]["error"] == "连接失败"

    # 空列表不崩
    page.update_servers([])
    assert page._body.count() >= 1


def test_server_dialog_parses_pairs(qapp):
    server = {
        "id": "fs", "name": "文件", "transport": "stdio", "command": "python",
        "args": ["a.py"], "url": None, "env": {"K": "V"},
        "headers_ref": {"Authorization": "vault://mcp_header/fs/token"}, "enabled": False,
    }
    dialog = ServerDialog(server)
    config = dialog.config()
    assert config["id"] == "fs"
    assert config["args"] == ["a.py"]
    assert config["env"] == {"K": "V"}
    assert config["headers_ref"]["Authorization"].startswith("vault://")
    assert config["enabled"] is False


def test_gate_dialog_decision(qapp):
    request = SimpleNamespace(call_id="c1", name="mcp.demo.echo", args={"text": "hi"}, permission="confirm")
    dialog = GateDialog(request)
    dialog.done(1)
    assert dialog.decision() is True
    dialog.done(0)
    assert dialog.decision() is False