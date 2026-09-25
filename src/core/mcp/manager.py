"""MCP 宿主管理（spec v0.0.3 rev41 / docs 07 §4.3）。

职责：server 生命周期（start/stop/reconnect）、initialize → tools/list → 注册、
availability 联动、状态机、宿主聚合状态、启停与调用的 append-only audit。

安全：MCP server 视为外部代码 —— 工具缺省权限 `confirm`；启停与调用只记
server/tool/ok/code，不记参数（参数原文只在关卡卡片呈现给用户）。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from shared.enums import Permission
from shared.errors import ErrorCode
from shared.redact import redact
from shared.schema import McpConfig, McpServerConfig

from core.mcp.security import McpFinding, McpSecurityScanner, get_scanner
from core.mcp.transport import MCPClient, McpTool, McpTransportError, create_client
from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec

MCP_TOOL_TIMEOUT_MS = 60000
MAX_OUTPUT_CHARS = 64000

log = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r"[^a-z0-9_]")

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_ERROR = "error"
STATE_STOPPING = "stopping"


def sanitize_component(value: str) -> str:
    """MCP 命名空间分量 sanitize：小写化，非 `[a-z0-9_]` 替 `_`。"""
    return _INVALID_CHARS.sub("_", (value or "tool").lower())


def _extract_output(result: dict) -> tuple[str, bool]:
    """从 MCP tools/call 结果提取文本输出与错误标记（输出限量）。"""
    is_error = bool(result.get("isError"))
    parts: list[str] = []
    for item in result.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif isinstance(item, dict) and item.get("type") == "resource":
            parts.append(str(item.get("resource", "")))
    text = "\n".join(p for p in parts if p)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n…（输出超长已截断）"
    return text, is_error


class McpManager:
    def __init__(
        self,
        registry: Registry,
        config_store: Any,
        secrets: Any,
        audit: Callable[..., None] | None = None,
        emit: Callable[[Any], None] | None = None,
        scanner: McpSecurityScanner | None = None,
    ) -> None:
        self._registry = registry
        self._config_store = config_store
        self._secrets = secrets
        self._audit = audit or (lambda *a, **k: None)
        self._emit = emit or (lambda *a, **k: None)
        # 安全检测为**预留**扩展点：默认 NullScanner，不做任何检测（v0.0.3）。
        self._scanner = scanner or get_scanner()
        self._configs: dict[str, McpServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._states: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._owned: dict[str, list[str]] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}

    # ---------- 配置 ----------
    def load(self) -> None:
        cfg: McpConfig = self._config_store.load("modules").mcp
        self._configs = {c.id: c for c in cfg.servers}
        for cid in self._configs:
            self._states.setdefault(cid, STATE_STOPPED)

    def _save(self) -> None:
        modules = self._config_store.load("modules")
        modules.mcp.servers = list(self._configs.values())
        self._config_store.save("modules", modules)

    # ---------- 生命周期（附加功能契约，切片 0）----------
    def activate(self) -> None:
        """装配：读配置 + 启动已启用 server（无启用 server 时零影响）。"""
        self.load()
        self.start_all()

    def deactivate(self) -> None:
        """真卸载：断开全部 server → 注销其工具（对象随即被 FeatureManager 断开引用）。"""
        self.shutdown()

    def start_all(self) -> None:
        for cid, cfg in self._configs.items():
            if cfg.enabled:
                self.start(cid)

    def start(self, server_id: str) -> None:
        cfg = self._configs.get(server_id)
        if cfg is None:
            return
        self._states[server_id] = STATE_STARTING
        self._emit_status(server_id)
        client = create_client(cfg, self._resolve_secret)
        self._clients[server_id] = client
        if not client.connect():
            self._states[server_id] = STATE_ERROR
            # 服务器可控文本（错误消息 / HTTPError 体）过脱敏再入事件（安全修订轮 F3）
            self._errors[server_id] = redact(client.last_error) or "连接失败"
            self._unregister_server(server_id)
            self._audit("mcp.server.start", id=server_id, transport=cfg.transport, ok=False)
            self._emit_status(server_id)
            return
        try:
            tools = client.list_tools()
        except McpTransportError as e:
            client.disconnect()
            self._states[server_id] = STATE_ERROR
            self._errors[server_id] = redact(str(e)) or "连接失败"
            self._unregister_server(server_id)
            self._audit("mcp.server.start", id=server_id, transport=cfg.transport, ok=False)
            self._emit_status(server_id)
            return
        self._register_tools(cfg, client, tools)
        self._states[server_id] = STATE_READY
        self._errors[server_id] = ""
        self._audit("mcp.server.start", id=server_id, transport=cfg.transport, ok=True)
        self._emit_status(server_id)
        self._emit_tools()

    def stop(self, server_id: str) -> None:
        self._states[server_id] = STATE_STOPPING
        client = self._clients.pop(server_id, None)
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - 停止路径尽力断开，失败不阻断注销
                log.debug("MCP 断开失败（忽略）", exc_info=True)
        self._unregister_server(server_id)
        self._states[server_id] = STATE_STOPPED
        self._errors[server_id] = ""
        self._audit("mcp.server.stop", id=server_id, ok=True)
        self._emit_status(server_id)
        self._emit_tools()

    def reconnect(self, server_id: str) -> None:
        self.stop(server_id)
        self.start(server_id)

    # ---------- 配置变更 ----------
    def upsert(self, config: McpServerConfig) -> None:
        if config.id in self._clients:
            self.stop(config.id)
        self._configs[config.id] = config
        self._states.setdefault(config.id, STATE_STOPPED)
        self._save()
        if config.enabled:
            self.start(config.id)
        self._emit_list()

    def delete(self, server_id: str) -> None:
        if server_id in self._clients:
            self.stop(server_id)
        self._configs.pop(server_id, None)
        self._states.pop(server_id, None)
        self._errors.pop(server_id, None)
        self._save()
        self._emit_list()

    def set_enabled(self, server_id: str, enabled: bool) -> None:
        cfg = self._configs.get(server_id)
        if cfg is None:
            return
        cfg.enabled = enabled
        self._save()
        if enabled:
            self.start(server_id)
        else:
            self.stop(server_id)
        self._emit_list()

    # ---------- 查询 ----------
    def list_status(self) -> list[dict]:
        out: list[dict] = []
        for cid, cfg in self._configs.items():
            out.append(
                {
                    "id": cid,
                    "name": cfg.name,
                    "enabled": cfg.enabled,
                    "transport": cfg.transport,
                    "state": self._states.get(cid, STATE_STOPPED),
                    "tools": list(self._owned.get(cid, [])),
                    "error": self._errors.get(cid) or None,
                }
            )
        return out

    def tool_entries(self) -> list[dict]:
        entries: list[dict] = []
        for name, (server_id, original) in self._tool_map.items():
            spec = next((s for s in self._registry.snapshot() if s.name == name), None)
            entries.append(
                {
                    "name": name,
                    "title": spec.title if spec else name,
                    "description": spec.description if spec else "",
                    "permission": spec.permission.value if spec else Permission.CONFIRM.value,
                    "server": server_id,
                    "original": original,
                }
            )
        return entries

    def host_state(self) -> str:
        enabled = [c for c in self._configs.values() if c.enabled]
        if not enabled:
            return "disabled"
        states = [self._states.get(c.id, STATE_STOPPED) for c in enabled]
        if all(s == STATE_READY for s in states):
            return "ready"
        if any(s == STATE_READY for s in states):
            return "degraded"
        return "error"

    def is_tool_available(self, server_id: str) -> bool:
        return self._states.get(server_id) == STATE_READY

    def scan(self, server_id: str, checks: list[str] | None = None) -> list[McpFinding]:
        """对已配置服务器做一次安全体检（v0.0.9）。

        findings 仅供展示，不参与权限/能力判定；审计只记检测项计数，不记证据原文。
        无活连接时主动探测项自动记 `skip`（不报错）。
        """
        cfg = self._configs.get(server_id)
        if cfg is None:
            return []
        client = self._clients.get(server_id)
        try:
            findings = self._scanner.scan(cfg, client, checks)
        except Exception:
            log.exception("MCP 安全检测失败：%s", server_id)
            findings = []
        counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
        for finding in findings:
            counts[finding.status] = counts.get(finding.status, 0) + 1
        self._audit("mcp.scan", id=server_id, transport=cfg.transport, **counts)
        return findings

    def scan_report(self, server_id: str, checks: list[str] | None = None) -> dict | None:
        """体检结果载荷（服务器不存在返回 None）；findings 证据已脱敏。"""
        cfg = self._configs.get(server_id)
        if cfg is None:
            return None
        findings = self.scan(server_id, checks)
        summary = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
        for finding in findings:
            summary[finding.status] = summary.get(finding.status, 0) + 1
        return {
            "server_id": server_id,
            "server_name": cfg.name,
            "findings": [f.as_dict() for f in findings],
            "summary": summary,
        }

    # ---------- 内部 ----------
    def _resolve_secret(self, name: str) -> str | None:
        try:
            return self._secrets.get(name)
        except Exception:
            return None

    def _register_tools(self, cfg: McpServerConfig, client: MCPClient, tools: list[McpTool]) -> None:
        self._unregister_server(cfg.id)
        owned: list[str] = []
        sid = sanitize_component(cfg.id)
        used: set[str] = set()
        for tool in tools:
            base = f"mcp.{sid}.{sanitize_component(tool.name)}"
            name = base
            ordinal = 2
            while name in used or any(name in v for v in self._owned.values()):
                name = f"{base}_{ordinal}"
                ordinal += 1
            used.add(name)
            permission = self._permission_for(cfg, name, tool.name)
            spec = ToolSpec(
                name=name,
                title=f"{cfg.name} · {tool.name}",
                description=tool.description,
                permission=permission,
                input_schema=tool.input_schema or {},
                timeout_ms=MCP_TOOL_TIMEOUT_MS,
                availability=lambda cid=cfg.id: self.is_tool_available(cid),
            )
            self._registry.register(spec, self._make_handler(client, cfg.id, tool.name))
            self._tool_map[name] = (cfg.id, tool.name)
            owned.append(name)
        self._owned[cfg.id] = owned

    def _permission_for(self, cfg: McpServerConfig, name: str, original: str) -> Permission:
        overrides = cfg.tool_permissions or {}
        raw = overrides.get(name) or overrides.get(original)
        try:
            return Permission(raw) if raw else Permission.CONFIRM
        except ValueError:
            return Permission.CONFIRM

    def _unregister_server(self, server_id: str) -> None:
        for name in self._owned.pop(server_id, []):
            self._registry.unregister(name)
            self._tool_map.pop(name, None)

    def _make_handler(self, client: MCPClient, server_id: str, original: str) -> Callable[[dict, Any], ToolResult]:
        def handler(args: dict, ctx: Any = None) -> ToolResult:
            start = time.monotonic()
            try:
                result = client.call_tool(original, args)
            except McpTransportError as e:
                self._audit("tool.call", server=server_id, tool=original, ok=False, code=e.code)
                return ToolResult(
                    ok=False, error={"code": e.code, "message": redact(str(e)) or ""},
                    duration_ms=_ms(start),
                )
            except Exception as e:  # noqa: BLE001 - 后端异常统一归码
                self._audit(
                    "tool.call", server=server_id, tool=original, ok=False,
                    code=ErrorCode.TOOL_BACKEND_ERROR.value,
                )
                return ToolResult(
                    ok=False,
                    error={"code": ErrorCode.TOOL_BACKEND_ERROR.value, "message": redact(str(e)) or ""},
                    duration_ms=_ms(start),
                )
            output, is_error = _extract_output(result if isinstance(result, dict) else {})
            # MCP 工具输出是持久化通道（tool.result → events.jsonl）：必须接入三层脱敏（F5）
            output = redact(output) or output
            self._audit(
                "tool.call", server=server_id, tool=original, ok=not is_error,
                code=ErrorCode.TOOL_BACKEND_ERROR.value if is_error else None,
            )
            if is_error:
                return ToolResult(
                    ok=False,
                    output=output,
                    error={"code": ErrorCode.TOOL_BACKEND_ERROR.value, "message": "工具后端返回错误"},
                    duration_ms=_ms(start),
                )
            return ToolResult(ok=True, output=output, duration_ms=_ms(start))

        return handler

    def shutdown(self) -> None:
        for server_id in list(self._clients):
            self.stop(server_id)

    # ---------- 事件 ----------
    def _emit_status(self, server_id: str) -> None:
        from shared.envelope import McpServerStatus

        self._emit(
            McpServerStatus(
                id=server_id,
                state=self._states.get(server_id, STATE_STOPPED),
                tools=list(self._owned.get(server_id, [])),
                error=redact(self._errors.get(server_id)) or None,  # 防御性再脱敏（F3）
            )
        )

    def _emit_list(self) -> None:
        from shared.envelope import McpServerList

        self._emit(McpServerList(servers=self.list_status()))

    def _emit_tools(self) -> None:
        from shared.envelope import ToolList

        self._emit(ToolList(tools=self.tool_entries()))


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)