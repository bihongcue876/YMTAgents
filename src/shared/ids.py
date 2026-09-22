"""ID 生成：<前缀>_<uuid7>（docs 03 §1）。

uuid7 时间有序，目录名即天然按创建排序。Python 3.14 提供标准库 uuid.uuid7。
"""

from __future__ import annotations

import uuid

# 规范前缀（docs 03 §1）
SESS = "sess"
PRS = "prs"
LIB = "lib"
SKL = "skl"
PRV = "prv"
#: v0.0.6：工作区。默认工作区用固定值 `WS_DEFAULT`（不是 uuid，以便登记表可读、可手编）。
WS = "ws"
WS_DEFAULT = "ws_default"


def new_uuid7() -> uuid.UUID:
    """返回一个 uuid7。"""
    return uuid.uuid7()


def new_id(prefix: str) -> str:
    """生成 <prefix>_<uuid7> 形式的 ID。"""
    return f"{prefix}_{new_uuid7()}"
