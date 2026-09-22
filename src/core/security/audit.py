"""审计回调的安全调用（单一来源）。

审计（audit append-only 事件）失败**不得影响主流程**（安全铁律：审计是旁路），
但也不得无痕 —— 记 debug 日志（与 rev54 的 MCP 吞异常补日志同原则）。
原先 `vault.py` 与 `legacy.py` 各有一份等价的 try/except-pass 实现，收编于此。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

AuditFn = Callable[..., None]


def safe_audit(audit: AuditFn | None, action: str, **fields: Any) -> None:
    """调用审计回调；回调为 None 或抛异常都不影响主流程（失败记 debug）。"""
    if audit is None:
        return
    try:
        audit(action, **fields)
    except Exception:  # noqa: BLE001 - 审计是旁路，失败不阻断主流程
        log.debug("audit 回调失败 action=%s", action, exc_info=True)
