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


def new_uuid7() -> uuid.UUID:
    """返回一个 uuid7。"""
    return uuid.uuid7()


def new_id(prefix: str) -> str:
    """生成 <prefix>_<uuid7> 形式的 ID。"""
    return f"{prefix}_{new_uuid7()}"
