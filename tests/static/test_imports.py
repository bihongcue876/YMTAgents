"""依赖方向静态检查（docs 04 §1）。

规则：
1. 项目内一律使用绝对导入（禁止 `from . import x` / `from ..pkg import x`）。
2. 每个包只允许依赖 docs 04 §1 表中列出的项目内包。
3. `shared` 只允许标准库（禁止 import 项目内任何包）。

违规即测试失败（作为构建门禁）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PROJECT_TOP = {"app", "core", "gui", "shared"}

# 允许依赖（None = 任意）
ALLOWED: dict[str, set[str] | None] = {
    "shared": set(),
    "core.bus": {"shared"},
    "core.store": {"shared", "core.bus"},
    "core.security": {"shared", "core.store"},
    "core.gateway": {"shared", "core.bus", "core.store", "core.security"},
    "core.registry": {"shared", "core.bus"},
    # 检索模块轮：MCP 与 retrieval 共用 core.httputil（禁重定向 + 限量读取，单源）
    "core.mcp": {"shared", "core.registry", "core.httputil"},
    "core.shell": {"shared", "core.registry"},
    # 安全修订轮：skills 复用 core.store.atomic（原子写单源）+ git 出口过 Whitelist
    "core.skills": {"shared", "core.registry", "core.store", "core.gateway"},
    "core.memory": {"shared", "core.bus"},
    "core.workspace": {"shared", "core.store", "core.files"},
    "core.files": {"shared", "core.bus", "core.registry", "core.store"},
    # 检索模块（spec-2026-09-25-retrieval）：httputil 为 core 内共用抓取层；
    # 密钥经注入 resolver，不 import core.security。
    "core.retrieval": {"shared", "core.registry", "core.gateway", "core.store", "core.httputil", "core.modules"},
    "core.agent": {
        "shared",
        "core.bus",
        "core.gateway",
        "core.registry",
        "core.memory",
        "core.store",
    },
    "core.modules": {"shared", "core.bus", "core.registry"},
    "core.modules.btcm": {"shared", "core.gateway", "core.registry", "core.modules"},
    "core.modules.dpim": {
        "shared", "core.gateway", "core.registry", "core.modules", "core.library"
    },
    "core.library": {"shared", "core.store"},
    "gui": {"shared", "core.bus"},
    "app": None,
}


def pkg_of(path: Path) -> str:
    rel = path.relative_to(SRC).parts
    if rel[0] == "core" and len(rel) > 3 and rel[1] == "modules":
        return f"core.modules.{rel[2]}"
    if rel[0] == "core" and len(rel) > 2:
        return f"core.{rel[1]}"
    return rel[0]


def import_target(name: str) -> str | None:
    """把项目内导入名归一为包标识，如 core.bus.bridge -> core.bus。"""
    parts = name.split(".")
    if parts[0] not in PROJECT_TOP:
        return None
    if parts[0] == "core":
        if len(parts) > 2 and parts[1] == "modules" and parts[2] in {"btcm", "dpim"}:
            return f"core.modules.{parts[2]}"
        return f"core.{parts[1]}" if len(parts) > 1 else "core"
    return parts[0]


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_relative_imports() -> None:
    bad: list[str] = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                bad.append(f"{path}:{node.lineno}")
    assert not bad, "禁止相对导入：\n" + "\n".join(bad)


def test_dependency_direction() -> None:
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        owner = pkg_of(path)
        allowed = ALLOWED.get(owner, set())
        if allowed is None:
            continue
        for node in ast.walk(_parse(path)):
            names: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                names = [(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "", node.lineno)]
            for name, lineno in names:
                target = import_target(name)
                if target is None:
                    continue
                if target == owner or target.startswith(owner + "."):
                    continue  # 包内自引用
                if target not in allowed:
                    violations.append(f"{owner} -> {target}  ({path}:{lineno})")
    assert not violations, "依赖方向违规：\n" + "\n".join(violations)
