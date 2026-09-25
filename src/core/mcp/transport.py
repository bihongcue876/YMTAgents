"""MCP 传输层（spec v0.0.3 §2 / docs 07 §4.3）。

自研轻量 JSON-RPC 2.0 客户端，零新增依赖（HTTP/SSE 走标准库 urllib）。
三传输：
- `stdio`：行分隔 JSON 子进程（stdin/stdout 管道）。
- `sse`：GET 建立事件流，POST 发送请求，响应经事件流返回。
- `http`：Streamable HTTP 单端点 POST（响应兼容 JSON 与 SSE）。

安全铁律：
- 非本机地址必须 https（`shared.net.is_secure_transport`），否则拒绝连接；
- `headers_ref` 只存 `vault://<name>`，值经 `secret_resolver` 从 Vault 解析，永不落盘/入日志。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from core.httputil import MAX_RESPONSE_BYTES, NO_REDIRECT_OPENER, NoRedirect as _NoRedirect
from core.httputil import open_bounded as _open_bounded
from shared.errors import ErrorCode
from shared.net import is_secure_transport
from shared.schema import McpServerConfig

# MCP 协议版本：客户端支持的全部版本，从新到旧排序（ISO 日期可字典序比较）
SUPPORTED_PROTOCOL_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18"]
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
MODERN_PROTOCOL_START = "2026-07-28"
META_PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO = {"name": "YMTAgents", "version": "0.0.3"}

VAULT_PREFIX = "vault://"

log = logging.getLogger(__name__)

_PYTHON_COMMANDS = {"python", "pythonw", "python3", "pythonw3", "py"}

#: SSE 行 / stdio 行的字节上限（安全修订轮 F4）：
#: 恶意服务器不得用无限长响应打满内存。
#: （响应体上限 MAX_RESPONSE_BYTES 与禁重定向 opener 已提炼到 core.httputil 共用。）
MAX_SSE_LINE_BYTES = 1_048_576
MAX_STDIO_LINE_BYTES = 8_388_608

SecretResolver = Callable[[str], str | None]


class McpTransportError(RuntimeError):
    """传输/协议层异常，携带错误码（归码见 docs 09 §3）。"""

    def __init__(self, message: str, code: str = ErrorCode.TOOL_BACKEND_ERROR.value) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class McpTool:
    """MCP 服务器声明的单个工具（未 sanitize 的原始名）。"""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)


def secret_ref_name(ref: str) -> str:
    """把 `vault://<name>` 引用还原为 Vault 中的存储名。"""
    return ref[len(VAULT_PREFIX):] if ref.startswith(VAULT_PREFIX) else ref


def resolve_command(command: str) -> str | None:
    """把配置中的命令解析为可直接启动的完整路径。

    Windows 上裸命令名（如 "python"）可能被系统「应用执行别名」抢先解析到
    Microsoft Store Python，绕过 PATH 顺序；解释器类命令优先使用当前进程解释器
    （MCP 服务器依赖多安装在当前 venv），其余命令按 PATH 解析。
    """
    if not command:
        return None
    if Path(command).is_absolute():
        return command
    if command.lower() in _PYTHON_COMMANDS:
        if not getattr(sys, "frozen", False):
            return sys.executable
        return shutil.which(command)
    return shutil.which(command)


class MCPClient(ABC):
    """MCP 客户端抽象基类：统一 JSON-RPC 信封与握手，各传输只实现 `_send`。"""

    def __init__(self, config: McpServerConfig, secret_resolver: SecretResolver | None = None) -> None:
        self.config = config
        self._secret_resolver = secret_resolver
        self._connected = False
        self._protocol_version = ""
        self._req_id = 0
        self.last_error = ""

    @property
    def protocol_version(self) -> str:
        return self._protocol_version

    def _is_modern(self) -> bool:
        return bool(self._protocol_version) and self._protocol_version >= MODERN_PROTOCOL_START

    # ---------- 内部辅助 ----------
    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _build_request(self, method: str, params: dict | None = None) -> dict:
        req: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            req["params"] = params
        if self._is_modern():
            meta = req.setdefault("params", {}).setdefault("_meta", {})
            meta[META_PROTOCOL_KEY] = self._protocol_version
            meta.setdefault(META_CAPABILITIES_KEY, {})
        return req

    def _build_notification(self, method: str, params: dict | None = None) -> dict:
        notif: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            notif["params"] = params
        return notif

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        resp = self._send(self._build_request(method, params))
        if resp is None:
            raise McpTransportError(f"请求{method}未获得响应")
        if "error" in resp:
            err = resp["error"]
            raise McpTransportError(f"MCP错误[{err.get('code')}]: {err.get('message')}")
        return resp.get("result", {})

    def _resolve_headers(self) -> dict[str, str]:
        """把 headers_ref 解析为真实请求头；任一凭据不可解即 fail-closed。"""
        headers: dict[str, str] = {}
        for name, ref in (self.config.headers_ref or {}).items():
            value = self._secret_resolver(secret_ref_name(ref)) if self._secret_resolver else None
            if not value:
                raise McpTransportError(
                    f"凭据不可用：{name}", ErrorCode.SECRET_UNAVAILABLE.value
                )
            headers[name] = value
        return headers

    def _check_secure(self) -> str:
        """校验 url 传输保密性；返回空串表示通过，否则为失败原因。

        stdio 无网络端点，不做传输保密性校验；http/sse 必须有 url 且非本机须 https。
        """
        if self.config.transport == "stdio":
            return ""
        url = self.config.url or ""
        if not url:
            return "缺少 url"
        if not is_secure_transport(url):
            return "明文传输不安全：非本机地址必须使用 https://"
        return ""

    # ---------- 子类实现 ----------
    @abstractmethod
    def _send(self, message: dict) -> dict | None:
        """发送消息并返回响应；通知（无 id）返回 None。"""

    # ---------- 公共接口 ----------
    def connect(self) -> bool:
        if (reason := self._check_secure()):
            self._connected = False
            self.last_error = reason
            return False
        self.last_error = ""
        try:
            negotiated = self._negotiate_version()
            self._protocol_version = negotiated
            self._connected = True
            if not self._is_modern():
                try:
                    self._send(self._build_notification("notifications/initialized"))
                except Exception:  # noqa: BLE001 - 尽力通知，失败不阻断握手
                    log.debug("initialized 通知发送失败（忽略）", exc_info=True)
            return True
        except Exception as e:  # noqa: BLE001 - 连接失败一律降级为 False + last_error
            self._connected = False
            self.last_error = str(e)
            return False

    def _negotiate_version(self) -> str:
        """协商协议版本：优先 server/discover（现代），失败回退 initialize（传统）。"""
        self._protocol_version = DEFAULT_PROTOCOL_VERSION
        try:
            result = self._rpc("server/discover", {})
            version = result.get("protocolVersion", "")
            if not version:
                supported = result.get("supportedVersions") or []
                for v in SUPPORTED_PROTOCOL_VERSIONS:
                    if v in supported:
                        version = v
                        break
            if version:
                return version
        except McpTransportError:
            pass

        self._protocol_version = ""
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            params = {"protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT_INFO}
            try:
                result = self._rpc("initialize", params)
                server_version = result.get("protocolVersion", "")
                if server_version:
                    return server_version
            except McpTransportError as e:
                if "nsupported protocol version" in str(e).lower():
                    continue
                raise
        raise McpTransportError("无法与服务器协商出共同支持的协议版本")

    def disconnect(self) -> None:
        self._connected = False

    def list_tools(self) -> list[McpTool]:
        result = self._rpc("tools/list")
        tools: list[McpTool] = []
        for t in result.get("tools", []):
            tools.append(
                McpTool(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}) or {},
                    annotations=t.get("annotations", {}) or {},
                )
            )
        return tools

    def call_tool(self, name: str, args: dict) -> dict:
        return self._rpc("tools/call", {"name": name, "arguments": args})

    def send_raw(self, message: dict) -> dict | None:
        """发送任意 JSON-RPC 消息（现代协议自动补 _meta 信封）。"""
        if self._is_modern():
            params = message.setdefault("params", {})
            if not isinstance(params, dict):
                params = {}
                message["params"] = params
            meta = params.setdefault("_meta", {})
            meta.setdefault(META_PROTOCOL_KEY, self._protocol_version)
            meta.setdefault(META_CAPABILITIES_KEY, {})
        return self._send(message)


class HttpMCPClient(MCPClient):
    """Streamable HTTP：单端点 POST，响应兼容 JSON 与 SSE（标准库 urllib）。"""

    def __init__(self, config: McpServerConfig, secret_resolver: SecretResolver | None = None) -> None:
        super().__init__(config, secret_resolver)
        self._session_id = ""

    def _timeout(self) -> float:
        return max(self.config.timeout_ms, 1) / 1000

    def _build_headers(self) -> dict:
        headers = dict(self._resolve_headers())
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json, text/event-stream")
        return headers

    def _send(self, message: dict) -> dict | None:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.config.url, data=data, headers=self._build_headers(), method="POST"
        )
        try:
            body, resp_headers = _open_bounded(req, self._timeout(), MAX_RESPONSE_BYTES)
            content_type = resp_headers.get("Content-Type", "")
            sid = resp_headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise McpTransportError(f"MCP HTTP错误[{e.code}]: {detail}") from e
        except urllib.error.URLError as e:
            raise McpTransportError(f"MCP 网络错误：{e.reason}") from e
        if "id" not in message:
            return None
        if "text/event-stream" in content_type:
            return self._parse_sse_response(body)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return self._parse_sse_response(body)

    @staticmethod
    def _parse_sse_response(text: str) -> dict | None:
        result = None
        for line in text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    try:
                        result = json.loads(data)
                    except json.JSONDecodeError:
                        continue
        return result

    def disconnect(self) -> None:
        super().disconnect()
        self._session_id = ""


class StdioMCPClient(MCPClient):
    """子进程 STDIO：行分隔 JSON（字节模式）。"""

    def __init__(self, config: McpServerConfig, secret_resolver: SecretResolver | None = None) -> None:
        super().__init__(config, secret_resolver)
        self._process: subprocess.Popen | None = None
        self._abort_error = ""

    def connect(self) -> bool:
        self._abort_error = ""
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            if self.config.env:
                env.update(self.config.env)
            command = resolve_command(self.config.command or "")
            if not command:
                self.last_error = f"无法解析命令：{self.config.command}"
                return False
            cmd = [command] + list(self.config.args)
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            self._process = None
            self.last_error = f"启动子进程失败：{e}"
            return False
        if self._process.poll() is not None:
            code = self._process.returncode
            self._abort()
            self.last_error = (
                f"STDIO子进程已退出(退出码{code})，请检查命令路径{self.config.command}与服务器运行依赖"
            )
            return False
        ok = super().connect()
        if not ok:
            self._abort()
        return ok

    def _abort(self, reason: str = "") -> None:
        if reason:
            self._abort_error = reason
        proc = self._process
        self._process = None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 - 清理路径尽力而为
            log.debug("MCP 子进程 terminate 失败（忽略）", exc_info=True)
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - 超时或已退出，升级为 kill
            log.debug("MCP 子进程 wait 超时，尝试 kill", exc_info=True)
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                log.debug("MCP 子进程 kill 失败（忽略）", exc_info=True)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                log.debug("MCP 管道关闭失败（忽略）", exc_info=True)

    def _send(self, message: dict) -> dict | None:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            if self._abort_error:
                raise McpTransportError(self._abort_error)
            raise McpTransportError("STDIO进程未启动或管道异常")
        line = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self._process.stdin.write(line)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            code = self._process.returncode if self._process is not None else None
            self._abort(f"STDIO写入失败（子进程可能已退出，退出码{code}）：{e}")
            raise McpTransportError(self._abort_error) from e
        if "id" not in message:
            return None
        while True:
            resp_line = self._process.stdout.readline()
            if not resp_line:
                self._abort("STDIO连接已关闭")
                raise McpTransportError(self._abort_error)
            try:
                return json.loads(resp_line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

    def _read_bounded_line(self) -> bytes:
        """带行长上限的读取（安全修订轮 F4）：恶意服务器单行塞 GB 级数据不得耗尽内存。

        注：超限按「协议被破坏」处理——先摘杀子进程再抛错；
        「连接不断但不发数据」的停滞阻塞由 executor 侧超时策略兜底（本期同轮记录）。
        """
        buf = bytearray()
        stream = self._process.stdout
        while True:
            chunk = stream.readline(65536)
            if not chunk:
                return bytes(buf) if buf else b""
            buf.extend(chunk)
            if chunk.endswith(b"\n"):
                return bytes(buf)
            if len(buf) > MAX_STDIO_LINE_BYTES:
                self._abort("STDIO 单行超长，疑似恶意服务器")
                raise McpTransportError(self._abort_error)

    def disconnect(self) -> None:
        super().disconnect()
        self._abort()


class SseMCPClient(MCPClient):
    """SSE：GET 建立事件流（endpoint 事件给出 POST 端点），响应经事件流返回。"""

    def __init__(self, config: McpServerConfig, secret_resolver: SecretResolver | None = None) -> None:
        super().__init__(config, secret_resolver)
        self._msg_endpoint = ""
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._endpoint_ready = threading.Event()
        self._pending: dict[int, dict] = {}
        self._pending_event = threading.Event()

    def _timeout(self) -> float:
        return max(self.config.timeout_ms, 1) / 1000

    def _build_headers(self) -> dict:
        headers = dict(self._resolve_headers())
        headers.setdefault("Accept", "text/event-stream")
        return headers

    def _same_origin(self, endpoint: str) -> bool:
        """端点与配置 URL 同 origin（scheme + host:port 均一致，大小写不敏感）。"""
        base = urlparse(self.config.url or "")
        target = urlparse(endpoint)
        return (base.scheme.lower(), base.netloc.lower()) == (
            target.scheme.lower(),
            target.netloc.lower(),
        )

    def connect(self) -> bool:
        self.last_error = ""
        self._stop.clear()
        self._endpoint_ready.clear()
        self._pending.clear()
        self._msg_endpoint = ""
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        if not self._endpoint_ready.wait(timeout=self.config.timeout_ms / 1000):
            self._abort()
            self.last_error = "SSE连接失败：未获取到消息端点"
            return False
        if self._stop.is_set():  # 端点被同源校验拒绝：直接失败，不走握手
            return False
        return super().connect()

    def _read_loop(self) -> None:
        try:
            req = urllib.request.Request(
                self.config.url, headers=self._build_headers(), method="GET"
            )
            with NO_REDIRECT_OPENER.open(req, timeout=self._timeout()) as resp:
                event = ""
                data = ""
                while True:
                    raw = resp.readline(MAX_SSE_LINE_BYTES)
                    if not raw or self._stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        self._handle_event(event, data)
                        event, data = "", ""
                    elif line.startswith("event:"):
                        event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data = line[len("data:"):].strip()
        except Exception as e:  # noqa: BLE001 - 事件流中断只记录，不影响主流程
            self.last_error = f"SSE事件流中断：{e}"
        self._stop.set()
        self._pending_event.set()

    def _handle_event(self, event: str, data: str) -> None:
        if event == "endpoint" and data:
            endpoint = urljoin(self.config.url or "", data)
            if not self._same_origin(endpoint):
                # 安全修订轮（F1）：服务端可给绝对 URL——urljoin 原样放行会让携带
                # vault 凭据头的 POST 发往任意主机/任意协议。只接受同 origin 端点。
                self._msg_endpoint = ""
                self.last_error = "SSE 消息端点与服务器地址不同源，已拒绝"
                self._stop.set()
                self._endpoint_ready.set()
                return
            self._msg_endpoint = endpoint
            self._endpoint_ready.set()
        elif event == "message" and data:
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                return
            rid = msg.get("id") if isinstance(msg, dict) else None
            if rid is not None:
                self._pending[rid] = msg
                self._pending_event.set()

    def _send(self, message: dict) -> dict | None:
        if not self._msg_endpoint:
            raise McpTransportError("SSE未就绪：缺少消息端点")
        rid = message.get("id")
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._msg_endpoint, data=data, headers=self._build_headers(), method="POST"
        )
        try:
            body, _resp_headers = _open_bounded(req, self._timeout(), MAX_RESPONSE_BYTES)
            content_type = _resp_headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise McpTransportError(f"MCP SSE错误[{e.code}]: {detail}") from e
        except urllib.error.URLError as e:
            raise McpTransportError(f"MCP 网络错误：{e.reason}") from e
        if "id" not in message:
            return None
        if "application/json" in content_type and rid is not None:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and parsed.get("id") == rid:
                    return parsed
            except json.JSONDecodeError:
                pass
        while not self._stop.is_set():
            if rid in self._pending:
                return self._pending.pop(rid)
            self._pending_event.wait(timeout=0.5)
            self._pending_event.clear()
        raise McpTransportError(f"SSE请求未获得响应：{self.last_error}")

    def disconnect(self) -> None:
        super().disconnect()
        self._abort()

    def _abort(self) -> None:
        self._stop.set()
        self._pending_event.set()
        self._msg_endpoint = ""
        self._pending.clear()


def create_client(config: McpServerConfig, secret_resolver: SecretResolver | None = None) -> MCPClient:
    """按 transport 创建对应客户端。"""
    if config.transport == "http":
        return HttpMCPClient(config, secret_resolver)
    if config.transport == "sse":
        return SseMCPClient(config, secret_resolver)
    if config.transport == "stdio":
        return StdioMCPClient(config, secret_resolver)
    raise ValueError(f"未知传输类型: {config.transport}")