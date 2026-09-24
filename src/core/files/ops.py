"""内置文件操作实现：读 / 查 / 写 / 改（spec-2026-09-24-file-tools §3 / §11）。

四条不变式：
1. **有界**：遍历用 os.scandir + 剪枝（禁止目录不进入），条目 / 命中 / 字符三重上限；
2. **流式**：grep 逐行、read 只留窗口 —— 大文件不整份进内存；
3. **行尾保持**（D-11）：匹配与展示统一按 LF，写回时恢复文件原行尾；
4. **原子**：写经 core.store.atomic（tmp + os.replace），覆盖前留一代 .bak（D-13）。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Iterator

from core.files.paths import (
    FileAmbiguous,
    FileMissing,
    FileNoMatch,
    FilePathError,
    is_denied_dir,
    is_denied_name,
    is_hidden_name,
    is_private_dir,
)
from core.store.atomic import apply_eol, atomic_write_text_exact, backup_file, detect_eol

DEFAULT_READ_LIMIT = 2000
MAX_READ_LIMIT = 5000
#: 单次返回的字符预算（256 K）：超过只截断、不报错。
MAX_READ_BYTES = 262_144
#: 超过此体量只读头部（避免把巨型文件整份读进内存）。
_FULL_SCAN_MAX_BYTES = 2_097_152
MAX_LINE_CHARS = 2000
MAX_GLOB_RESULTS = 100
MAX_GREP_RESULTS = 250
MAX_SCAN_ENTRIES = 20_000
MAX_SCAN_MATCHES = 2_000
MAX_SEARCH_FILE_BYTES = 2_097_152
MAX_SEARCH_LINE_CHARS = 300
TRUNCATION_MARK = "\n\n…[内容已截断]"
LONG_LINE_MARK = " …[本行已截断]"


def _read_raw(path: Path) -> str:
    """读全文（严格 UTF-8、拒二进制）。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FilePathError("读取文件失败。") from exc
    if b"\x00" in data[:4096]:
        raise FilePathError("该文件不是文本文件（含 NUL 字节），不予读取。")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FilePathError("该文件不是 UTF-8 文本，不予读取。") from exc


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        raise FileMissing("目标不是已存在的普通文件。")


def _clip_line(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    return line[:MAX_LINE_CHARS] + LONG_LINE_MARK


def read_text(path: Path, *, offset: int = 1, limit: int = DEFAULT_READ_LIMIT) -> dict:
    """读文本并按行号窗口返回（不报错、只截断）。

    小文件全扫以获得**精确**总行数；大文件（> 2 MB）只读头部并如实标注「总数未知」。
    """
    start = max(1, int(offset or 1))
    count = max(1, min(int(limit or DEFAULT_READ_LIMIT), MAX_READ_LIMIT))
    end = start + count - 1
    size = _size_of(path)

    if size <= _FULL_SCAN_MAX_BYTES:
        text = _read_raw(path).replace("\r\n", "\n")
        lines = text.split("\n")
        total: int | None = len(lines)
        window = lines[start - 1:end]
        total_known = True
    else:
        with path.open("r", encoding="utf-8", newline="") as handle:
            head: list[str] = []
            used = 0
            index = 0
            for line in handle:
                index += 1
                piece = _clip_line(line.replace("\r\n", "\n").rstrip("\n"))
                if used + len(piece) > MAX_READ_BYTES:
                    break
                head.append(piece)
                used += len(piece) + 1
        total = None
        total_known = False
        window = head[start - 1:end]

    window = [_clip_line(line) for line in window]
    shown_to = start - 1 + len(window)
    numbered = "\n".join(f"{start + i}| {line}" for i, line in enumerate(window))
    truncated = (not total_known) or (total is not None and total > shown_to) or size > MAX_READ_BYTES
    if len(numbered) > MAX_READ_BYTES:
        numbered = numbered[:MAX_READ_BYTES] + TRUNCATION_MARK
        truncated = True
    return {
        "text": numbered,
        "total_lines": total,
        "total_known": total_known,
        "shown_from": start,
        "shown_to": shown_to,
        "truncated": truncated,
        "bytes": size,
    }


def _walk(base: Path, *, data_root: Path, include_hidden: bool) -> Iterator[Path]:
    """有界遍历普通文件：剪枝禁止目录与（默认）隐藏项，符号链接不跟随。"""
    stack = [base]
    scanned = 0
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > MAX_SCAN_ENTRIES:
                return
            name = entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if is_denied_dir(name) or is_private_dir(entry.path, data_root):
                        continue
                    if not include_hidden and is_hidden_name(name):
                        continue
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if not include_hidden and is_hidden_name(name):
                continue
            if is_denied_name(name):
                continue
            yield Path(entry.path)


def _display(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def glob_files(pattern: str, *, base: Path, data_root: Path,
               include_hidden: bool = False, limit: int = MAX_GLOB_RESULTS) -> dict:
    """按路径模式列出**文件**（绝不列目录）；修改时间倒序；有界。"""
    text = str(pattern or "").strip()
    if not text:
        raise FilePathError("pattern 不能为空。")
    deep = "/" in text or "\\" in text
    pattern_text = text.replace("\\", "/")
    found: list[tuple[float, str]] = []
    overflow = False
    for path in _walk(base, data_root=data_root, include_hidden=include_hidden):
        relative = _display(path, base)
        target = relative if deep else path.name
        if not fnmatch.fnmatch(target, pattern_text):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        found.append((mtime, relative))
        if len(found) >= MAX_SCAN_MATCHES:
            overflow = True
            break
    found.sort(key=lambda item: item[0], reverse=True)
    kept = found[: max(1, int(limit))]
    if len(found) > len(kept):
        overflow = True
    return {"files": [{"path": rel, "mtime": mtime} for mtime, rel in kept],
            "count": len(kept), "overflow": overflow}


def grep_files(pattern: str, *, base: Path, data_root: Path, include: str = "",
               include_hidden: bool = False, limit: int = MAX_GREP_RESULTS) -> dict:
    """正则搜索（stdlib re，逐行流式）：命中行 + 行号 + 文件；有界。"""
    text = str(pattern or "").strip()
    if not text:
        raise FilePathError("pattern 不能为空。")
    try:
        engine = re.compile(text)
    except re.error as exc:
        raise FilePathError(f"正则不受支持：{exc}") from exc
    include_text = str(include or "").strip()
    hits: list[dict] = []
    files_scanned = 0
    overflow = False
    for path in _walk(base, data_root=data_root, include_hidden=include_hidden):
        relative = _display(path, base)
        if include_text and not fnmatch.fnmatch(path.name, include_text):
            continue
        try:
            if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
        except OSError:
            continue
        files_scanned += 1
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                for number, line in enumerate(handle, start=1):
                    stripped = line.replace("\r\n", "\n").rstrip("\n")
                    if not engine.search(stripped):
                        continue
                    hits.append({"path": relative, "line": number,
                                 "text": stripped[:MAX_SEARCH_LINE_CHARS]})
                    if len(hits) > max(1, int(limit)):
                        overflow = True
                        break
        except (OSError, UnicodeDecodeError):
            continue
        if overflow:
            break
    kept = hits[: max(1, int(limit))]
    return {"matches": kept, "count": len(kept), "files_scanned": files_scanned,
            "overflow": overflow}


def edit_text(path: Path, old_string: str, new_string: str, *, replace_all: bool = False) -> dict:
    """字面替换（默认要求唯一命中）；原子写 + 行尾保持。"""
    old = str(old_string or "").replace("\r\n", "\n")
    if not old:
        raise FilePathError("old_string 不能为空。")
    new = str(new_string or "").replace("\r\n", "\n")
    raw = _read_raw(path)
    eol = detect_eol(raw)
    text = raw.replace("\r\n", "\n")
    hits = text.count(old)
    if hits == 0:
        raise FileNoMatch("未找到要替换的文本（字面匹配，区分空白与大小写）。")
    if hits > 1 and not replace_all:
        raise FileAmbiguous(f"该片段命中 {hits} 处；请给出更长的唯一片段，或显式 replace_all。")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    replaced = hits if replace_all else 1
    if updated == text:
        return {"path": str(path), "noop": True, "replacements": 0, "added": 0, "removed": 0}
    _write_exact(path, updated, eol=eol, backup=True)
    added, removed = _diff_stat(text, updated)
    return {"path": str(path), "noop": False, "replacements": replaced,
            "added": added, "removed": removed}


def write_text(path: Path, content: str) -> dict:
    """整文件写入（新建或覆盖）：原子、保持既有行尾、不自动建父目录。"""
    if not path.parent.is_dir():
        raise FilePathError("目标目录不存在（不自动创建目录）。")
    text = str(content if content is not None else "").replace("\r\n", "\n")
    if path.exists():
        if not path.is_file():
            raise FilePathError("目标不是普通文件。")
        eol = detect_eol(_read_raw(path))
        _write_exact(path, text, eol=eol, backup=True)
        created = False
    else:
        _write_exact(path, text, eol="\n", backup=False)
        created = True
    return {"path": str(path), "created": created, "bytes": len(text.encode("utf-8")),
            "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


def _write_exact(path: Path, text_lf: str, *, eol: str, backup: bool) -> None:
    if backup:
        try:
            backup_file(path)
        except OSError:  # 备份失败不阻断写入（写入本身仍原子）
            pass
    try:
        atomic_write_text_exact(path, apply_eol(text_lf, eol))
    except OSError as exc:
        raise FilePathError("写入文件失败。") from exc


def _diff_stat(before: str, after: str) -> tuple[int, int]:
    """(新增行数, 删除行数)：按行多重集差的最小估计（仅供对照卡展示）。"""
    old_lines = before.split("\n")
    new_lines = after.split("\n")
    common = 0
    matched: list[str] = list(new_lines)
    for line in old_lines:
        if line in matched:
            matched.remove(line)
            common += 1
    added = len(new_lines) - common
    removed = len(old_lines) - common
    return added, removed
