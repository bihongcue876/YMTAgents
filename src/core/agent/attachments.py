"""工作区附件解析：相对子路径、有界 UTF-8 读取、默认工作区向后兼容。"""

from __future__ import annotations

import os
from pathlib import Path

from shared.redact import redact

MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_CHARS = 1_000_000
_READ_TRUNCATION = "\n…[文件读取已截断]"


class AttachmentError(ValueError):
    """附件路径、类型、编码或数量不符合边界约束。"""


def _safe_file(root: Path, relative: str) -> Path | None:
    text = str(relative or "").strip().replace("\\", "/")
    candidate = Path(text)
    if not text or candidate.is_absolute() or candidate.drive or candidate.root or ".." in candidate.parts:
        raise AttachmentError(f"附件路径无效：{relative}")
    try:
        lexical_base = Path(os.path.abspath(str(root)))
        if lexical_base.is_symlink():
            raise AttachmentError("工作区 root 不能是符号链接。")
        base = lexical_base.resolve(strict=True)
        if os.path.normcase(str(base)) != os.path.normcase(str(lexical_base)):
            raise AttachmentError("工作区 root 路径包含链接跳转。")
        current = base
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise AttachmentError(f"附件路径包含符号链接：{relative}")
        resolved = current.resolve(strict=True)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AttachmentError(f"无法访问附件：{relative}") from exc
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise AttachmentError(f"附件路径超出工作区：{relative}") from exc
    if not resolved.is_file():
        raise AttachmentError(f"附件不是普通文件：{relative}")
    return resolved


def load_attachments(
    workspace_root: Path,
    relative_paths: list[str],
    legacy_files_root: Path,
) -> list[tuple[str, str]]:
    """解析当前工作区优先、默认 `workspace/files` 回退的附件列表。"""
    if len(relative_paths) > MAX_ATTACHMENTS:
        raise AttachmentError(f"一次最多挂载 {MAX_ATTACHMENTS} 个文件。")
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in relative_paths:
        relative = str(raw or "").strip().replace("\\", "/")
        resolved = _safe_file(workspace_root, relative)
        if resolved is None:
            resolved = _safe_file(legacy_files_root, relative)
        if resolved is None:
            raise AttachmentError(f"附件未找到：{relative}")
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        try:
            with resolved.open("r", encoding="utf-8", errors="strict") as stream:
                content = stream.read(MAX_ATTACHMENT_CHARS + 1)
        except UnicodeDecodeError as exc:
            raise AttachmentError(f"附件不是有效 UTF-8 文本：{relative}") from exc
        except OSError as exc:
            raise AttachmentError(f"无法读取附件：{relative}") from exc
        if len(content) > MAX_ATTACHMENT_CHARS:
            content = content[:MAX_ATTACHMENT_CHARS] + _READ_TRUNCATION
        output.append((relative, redact(content) or ""))
    return output
