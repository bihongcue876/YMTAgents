"""书库 root 解析与禁设规则；路径只由 id / group_key / 明确 external root 决定。"""

from __future__ import annotations

import os
from pathlib import Path

from shared.schema import LibraryRecord

LIBRARIES_DIR = "libraries"
DATA_DIRNAME = ".ymtdata"


class LibraryPathError(ValueError):
    """root 编码错误或目录不可用。"""


class LibraryDenied(LibraryPathError):
    """命中禁设位置或被重复登记。"""


def abs_path(path: str | Path) -> Path:
    return Path(os.path.abspath(str(path)))


def path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_under(child: str | Path, parent: str | Path) -> bool:
    child_key = path_key(child)
    parent_key = path_key(parent).rstrip("\\/")
    return child_key == parent_key or child_key.startswith(parent_key + os.sep)


def validate_external_root(raw: str, data_root: str | Path) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise LibraryPathError("外部书库必须选择一个已存在的目录。")
    lexical = abs_path(text)
    if not lexical.is_absolute():
        raise LibraryPathError("外部书库目录必须是绝对目录。")
    if lexical.is_symlink():
        raise LibraryDenied("不接受符号链接作为书库 root。")
    try:
        resolved = lexical.resolve(strict=False)
    except OSError as exc:
        raise LibraryPathError("无法解析外部书库目录。") from exc
    if path_key(lexical) != path_key(resolved):
        raise LibraryDenied("外部书库路径包含链接跳转，无法确认实际落点。")

    data = abs_path(data_root)
    anchor = Path(resolved.anchor)
    if path_key(resolved) == path_key(anchor):
        raise LibraryDenied("不能把磁盘根目录设为书库。")
    if path_key(resolved) == path_key(Path.home()):
        raise LibraryDenied("不能把用户主目录直接设为书库。")
    if path_key(resolved) == path_key(data):
        raise LibraryDenied("不能把应用数据根设为书库。")
    if resolved.name.casefold() == DATA_DIRNAME.casefold():
        raise LibraryDenied("不能把 .ymtdata 目录本身设为书库。")
    if is_under(resolved, data) or is_under(data, resolved):
        raise LibraryDenied("外部书库不得位于应用数据根内，也不得包含应用数据根。")
    if not lexical.exists() or not lexical.is_dir():
        raise LibraryPathError("外部书库目录必须是已存在的目录。")
    return resolved


def library_root(record: LibraryRecord, data_root: str | Path) -> Path:
    """解析已登记书库实际数据目录；托管路径组件均为生成 ID。"""
    if record.root_kind == "external":
        if not record.root:
            raise LibraryPathError("外部书库缺少 root。")
        return validate_external_root(record.root, data_root)
    data = abs_path(data_root)
    base = data / LIBRARIES_DIR
    group = record.group_key
    if group is not None and not group.startswith("grp_"):
        raise LibraryPathError("书库 group_key 无效。")
    if not record.id.startswith("lib_"):
        raise LibraryPathError("书库 id 无效。")
    candidate = base / record.id if group is None else base / group / record.id
    resolved = candidate.resolve(strict=False)
    if not is_under(resolved, base.resolve(strict=False)):
        raise LibraryDenied("托管书库路径越出分配区。")
    return resolved


def ensure_regular_data_file(root: Path, filename: str) -> Path:
    """阻止 memory.db/graph.json 通过软链接写出已登记书库目录。"""
    if filename not in {"memory.db", "graph.json"}:
        raise LibraryPathError("不允许的书库文件名。")
    root = abs_path(root)
    target = root / filename
    if target.is_symlink():
        raise LibraryDenied(f"书库文件 {filename} 不得为符号链接。")
    return target
