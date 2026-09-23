"""MCP 工具安全检测（v0.0.9）。

填实 v0.0.3 预留的接缝：`McpSecurityScanner` 的默认实现由 `NullScanner` 换为 `McpScanner`。

- 检测**全手动触发**（请求 `mcp.scan`），运行于核心线程；
- findings 一律**只读展示**，永不参与权限裁定、可见性或工具派发（docs 07 §5）；
- 证据落盘 / 回显前一律过 `redact`，不含任何明文密钥；审计只记检测项计数。
"""

from core.mcp.security.checks import CHECKS, ItemResult
from core.mcp.security.scanner import (
    McpFinding,
    McpScanner,
    McpSecurityScanner,
    NullScanner,
    get_scanner,
)

__all__ = [
    "CHECKS",
    "ItemResult",
    "McpFinding",
    "McpScanner",
    "McpSecurityScanner",
    "NullScanner",
    "get_scanner",
]