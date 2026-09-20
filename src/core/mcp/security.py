"""MCP 安全检测扩展点（**预留**，v0.0.3 不实现）。

背景：`origins/MCP工具安全检测` 原型含一套 MCP 安全检查特色能力
（传输鉴权核查、工具注入/越权扫描等）。本项目**只移植配置与调用模式**，
安全检测部分在此**预留稳定扩展点**，待后续版本实现；现阶段不引入任何检测逻辑、
攻击载荷或扫描行为（用户裁决：注意现阶段不要做）。

接入方式（未来）：实现 `McpSecurityScanner` 并在 `McpManager` 注入；
`scan()` 的发现项仅供上层展示，**绝不**参与权限判定或能力扩展
（安全铁律：能力不可经数据扩展、参数原文只在关卡卡片呈现）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from shared.schema import McpServerConfig


@dataclass
class McpFinding:
    """一条安全发现（预留结构，当前无生产者）。"""

    code: str
    severity: str = "info"  # info | low | medium | high
    detail: str = ""


class McpSecurityScanner(ABC):
    """MCP server 安全检测器接口（**预留**）。"""

    @abstractmethod
    def scan(self, config: McpServerConfig) -> list[McpFinding]:
        """对单个 server 配置做安全核查，返回发现列表（不改变权限/能力）。"""


class NullScanner(McpSecurityScanner):
    """默认实现：不做任何检测（v0.0.3 预留期行为）。"""

    def scan(self, config: McpServerConfig) -> list[McpFinding]:
        return []


def get_scanner() -> McpSecurityScanner:
    """扫描器工厂（预留）：当前恒返回 `NullScanner`。"""
    return NullScanner()