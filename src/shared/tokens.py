"""token 估算（单一来源）。

「CJK 每字 1、其余约每 4 字符 1」是全部 token 预算计算（上下文组装、淘汰、
工具输出截断）的公共口径 —— 曾经在 `core.agent.context` 与 `core.registry.executor`
各留一份本地实现，靠 docstring 互相声明「口径一致」维持，属预算类逻辑的漂移风险，
故上收至 shared（纯函数、仅标准库，符合依赖方向 shared ← core）。
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 每字 1，其余约每 4 字符 1。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + (other + 3) // 4
