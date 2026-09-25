"""core.mcp.transport 单元测试（v0.0.3 rev40）。

覆盖：STDIO JSON-RPC 往返与协议协商、命令解析、命名空间 sanitize、
传输保密性 fail-closed、headers_ref 凭据不可解 fail-closed。
"""

from __future__ import annotations

from shared.schema import McpServerConfig

from core.mcp.transport import (
    StdioMCPClient,
    create_client,
    resolve_command,
    secret_ref_name,
)

#: 最小 MCP 服务器（STDIO 行分隔 JSON-RPC 2.0），实现现代 server/discover 与传统 initialize。
_FAKE_SERVER = r'''
import sys, json

def respond(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    rid = msg.get("id")
    if rid is None:
        continue  # notification
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "server/discover":
        respond(rid, {"protocolVersion": "2026-07-28", "supportedVersions": ["2026-07-28"]})
    elif method == "initialize":
        respond(rid, {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {}})
    elif method == "tools/list":
        respond(rid, {"tools": [{"name": "echo", "description": "回声", "inputSchema": {"type": "object"}}]})
    elif method == "tools/call":
        args = params.get("arguments") or {}
        respond(rid, {"content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}], "isError": False})
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not found"}}) + "\n")
        sys.stdout.flush()
'''


def test_resolve_command_and_secret_ref():
    assert secret_ref_name("vault://mcp_header/x/token") == "mcp_header/x/token"
    assert secret_ref_name("plain") == "plain"
    # 解释器类命令开发态解析到当前解释器（规避 Windows App Execution Alias 陷阱）
    assert resolve_command("python") is not None
    assert resolve_command("") is None


def test_stdio_roundtrip(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    cfg = McpServerConfig(
        id="demo", name="Demo", enabled=True, transport="stdio",
        command="python", args=[str(script)],
    )
    client = create_client(cfg)
    assert isinstance(client, StdioMCPClient)
    try:
        assert client.connect() is True
        assert client.protocol_version == "2026-07-28"  # 现代协商路径
        tools = client.list_tools()
        assert [t.name for t in tools] == ["echo"]
        assert tools[0].description == "回声"
        result = client.call_tool("echo", {"text": "hi"})
        assert result["content"][0]["text"] == "echo:hi"
    finally:
        client.disconnect()


def test_http_insecure_transport_rejected():
    cfg = McpServerConfig(id="x", name="X", transport="http", url="http://example.com/mcp")
    client = create_client(cfg)
    assert client.connect() is False
    assert "https" in client.last_error


def test_http_missing_headers_ref_fail_closed():
    cfg = McpServerConfig(
        id="x", name="X", transport="http", url="https://example.com/mcp",
        headers_ref={"Authorization": "vault://mcp_header/x/token"},
    )
    # 无 secret_resolver => 凭据不可解 => fail-closed（不发出请求）
    client = create_client(cfg)
    assert client.connect() is False
    assert "凭据不可用" in client.last_error


def test_unknown_transport_raises():
    cfg = McpServerConfig(id="x", name="X", transport="stdio")
    import pytest

    with pytest.raises(ValueError):
        create_client(cfg.model_copy(update={"transport": "bogus"}))


# ---- 安全修订轮（F1 / F2 / F4）-------------------------------------------


def test_no_redirect_handler_blocks_all_redirects():
    """F2：urllib 默认跟随重定向且带原凭据头——自建 opener 必须拒绝一切重定向。"""
    import urllib.request

    from core.mcp.transport import _NoRedirect

    handler = _NoRedirect()
    req = urllib.request.Request("https://a.example/mcp")
    assert handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/") is None
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://b.example/mcp") is None


def test_sse_endpoint_must_be_same_origin():
    """F1：服务端可给绝对 URL——不同 origin（跨主机或降级 http）一律拒绝。"""
    import pytest

    from core.mcp.transport import SseMCPClient

    cfg = McpServerConfig(id="x", name="X", transport="sse", url="https://example.com/sse")
    client = SseMCPClient(cfg)
    client._handle_event("endpoint", "http://evil.example/collect")
    assert client._msg_endpoint == "" and client._stop.is_set()
    # 同源相对路径接受
    client._stop.clear()
    client._handle_event("endpoint", "/messages")
    assert client._msg_endpoint == "https://example.com/messages"
    # 跨主机绝对地址拒绝
    client._stop.clear()
    client._handle_event("endpoint", "https://attacker.example/messages")
    assert client._msg_endpoint == ""