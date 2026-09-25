"""安全修订轮：注册表/执行器的安全收口（precheck 授信边界、allow 不提权、预览脱敏）。"""

from __future__ import annotations

import json

import pytest

from shared.enums import Permission

from core.registry.executor import ToolExecutor
from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec


def _noop_handler(args, ctx):
    return ToolResult(ok=True, output="ran")


def test_external_tool_namespaces_reject_precheck_and_preview():
    """MCP / 技能是外部代码：宿主策略保留位（precheck/preview）不得由其携带。"""
    for name in ("mcp.s1.echo", "skill.demo"):
        registry = Registry()
        with pytest.raises(ValueError):
            registry.register(
                ToolSpec(name=name, precheck=lambda args: ("allow", "")), _noop_handler
            )
        with pytest.raises(ValueError):
            registry.register(
                ToolSpec(name=name, preview=lambda args: {"old": "x"}), _noop_handler
            )
        # 不带保留位即可注册
        registry.register(ToolSpec(name=name), _noop_handler)
        assert registry.execute(name, {}, None).ok is True


def test_allow_precheck_never_overrides_restricted():
    """「从不提权」：restricted 声明档不吃 allow——策略层放行不越权。"""
    registry = Registry()
    registry.register(
        ToolSpec(name="shell.exec", permission=Permission.RESTRICTED,
                 precheck=lambda args: ("allow", "")),
        _noop_handler,
    )
    result = ToolExecutor(registry).execute("c1", "shell.exec", {})
    assert result.ok is False and result.error["code"] == "tool_denied"


def test_allow_precheck_skips_gate_for_confirm():
    """confirm + allow：跳过用户关卡正常执行（α 方案本意），gate 回调不被触碰。"""
    registry = Registry()
    registry.register(
        ToolSpec(name="file.read", precheck=lambda args: ("allow", "")),
        _noop_handler,
    )
    touched = []

    def _gate(*_a):
        touched.append(True)
        return False

    result = ToolExecutor(registry, gate=_gate).execute("c1", "file.read", {})
    assert result.ok is True and touched == []


def test_gate_preview_is_redacted():
    """预览会原样呈给用户：文件原文片段里的密钥形态必须先打码（防线纵深）。"""
    registry = Registry()
    registry.register(
        ToolSpec(name="file.write", preview=lambda args: {
            "kind": "write", "path": "notes.txt",
            "old": "api_key=sk-abcdef123456", "new": "",
        }),
        _noop_handler,
    )
    events = []
    executor = ToolExecutor(registry, emit=events.append, gate=lambda *_a: False)
    executor.execute("c1", "file.write", {"path": "notes.txt"})
    gates = [event for event in events if type(event).__name__ == "GateRequest"]
    assert gates, "confirm 工具应发出 GateRequest"
    preview = gates[0].preview
    assert "sk-abcdef123456" not in json.dumps(preview, ensure_ascii=False)
