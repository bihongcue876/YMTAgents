"""MCP 安全检测器：组装注册表、逐项执行、异常降级。

**只读铁律**：findings 为 DISPLAY-ONLY —— 永不参与权限裁定、工具可见性或派发。
本模块不得 import `core.mcp.manager`，也不得写任何 config / store。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from shared.redact import redact
from shared.schema import McpServerConfig

from core.mcp.security.checks import CHECKS, ItemResult

#: 状态 → 严重度映射。
_SEVERITY = {"fail": "high", "warn": "medium", "pass": "info", "skip": "info"}

#: 默认检测顺序。
_ORDER = ("A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4")


@dataclass(frozen=True)
class McpFinding:
    """单项检测结论（内部载体 + 事件载荷同形）。"""

    id: str
    name: str
    status: str  # pass | warn | fail | skip
    severity: str = "info"  # info | low | medium | high
    evidence: str = ""
    suggestion: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class McpSecurityScanner(ABC):
    """检测器接口：findings 只读展示，永不参与能力判定（docs 07 §5）。"""

    @abstractmethod
    def scan(
        self,
        config: McpServerConfig,
        client=None,
        checks: list[str] | None = None,
    ) -> list[McpFinding]:
        raise NotImplementedError


class NullScanner(McpSecurityScanner):
    """空实现（保留：供测试与「未启用检测」场景注入）。"""

    def scan(self, config, client=None, checks=None) -> list[McpFinding]:
        return []


class McpScanner(McpSecurityScanner):
    """真实检测器：逐项执行，单项异常降级为 warn，不中断整体。"""

    def scan(self, config, client=None, checks=None) -> list[McpFinding]:
        ids = [cid for cid in (checks or _ORDER) if cid in CHECKS]
        findings: list[McpFinding] = []
        for cid in ids:
            name, fn = CHECKS[cid]
            try:
                result: ItemResult = fn(config, client)
            except Exception as exc:  # 单项异常不拖垮整体（承原型范式）
                result = ItemResult(cid, name, "warn", f"检测异常：{type(exc).__name__}", "请人工复查该检测项")
            findings.append(
                McpFinding(
                    id=result.item_id,
                    name=result.item_name,
                    status=result.status,
                    severity=_SEVERITY.get(result.status, "info"),
                    evidence=redact(result.evidence or ""),
                    suggestion=result.suggestion or "",
                )
            )
        return findings


def get_scanner() -> McpSecurityScanner:
    """默认检测器（v0.0.9 起由 NullScanner 换为 McpScanner）。"""
    return McpScanner()