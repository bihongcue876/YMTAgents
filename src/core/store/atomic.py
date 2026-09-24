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


def atomic_write_text_exact(path: Path, text: str) -> None:
    """原子写入文本且**保持原行尾、不创建父目录**（v0.0.11 D-11）。

    与 atomic_write_text 的两点差异，都是文件工具的硬要求：
    - newline="" 不做换行翻译（CRLF 保持 CRLF）；
    - 不 mkdir：父目录不存在应由调用方判为参数错误，而不是被静默补齐。
    """
    tmp = _tmp_path(path)
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt >= _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_DELAYS[attempt])


def detect_eol(text: str) -> str:
    """文本换行风格：出现 CRLF 即判 CRLF，否则 LF。"""
    return "\r\n" if "\r\n" in text else "\n"


def apply_eol(text: str, eol: str) -> str:
    """把已归一为 LF 的文本恢复为指定行尾（LF 时原样返回）。"""
    if eol == "\n":
        return text
    return text.replace("\r\n", "\n").replace("\n", eol)


def eol_of_file(path: Path, *, sample: int = 65536) -> str:
    """目标文件既有的换行风格（只取样前 64 KB）；不存在或不可读则按 LF。"""
    try:
        with path.open("rb") as handle:
            raw = handle.read(sample)
    except OSError:
        return "\n"
    return detect_eol(raw.decode("utf-8", errors="ignore"))


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
