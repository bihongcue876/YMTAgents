"""原子写与单代备份（docs 03 §10）。

- 原子写：写临时文件 + os.replace，杜绝半写状态。
- 单代备份：程序改写前留同目录 .bak 一代（人工编辑不触发）。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

#: `os.replace` 撞 Windows 瞬时文件锁（杀软/索引器扫描刚落盘的 .tmp）时的有界重试。
#: 实测形态：WinError 5「拒绝访问」，毫秒级自行恢复（rev55 全量回归 3 例偶发）；
#: 毫秒退避重试 3 次，耗尽后照原样抛 —— fail-closed 不变。
_REPLACE_RETRIES = 3
_REPLACE_DELAYS = (0.05, 0.10, 0.20)


def _tmp_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".tmp")


def atomic_write_text(path: Path, text: str) -> None:
    """原子写入文本（UTF-8 / LF）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    tmp.write_text(text, encoding="utf-8", newline="\n")
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt >= _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_DELAYS[attempt])


def atomic_write_json(path: Path, obj: Any) -> None:
    """原子写入 JSON（缩进 2，非 ASCII 原样）。"""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def backup_file(path: Path) -> None:
    """若存在则复制为同目录 .bak 一代。"""
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def backup_and_write_json(path: Path, obj: Any) -> None:
    """先备份旧文件，再原子写 JSON。"""
    backup_file(path)
    atomic_write_json(path, obj)
