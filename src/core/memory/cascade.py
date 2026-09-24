"""AGENTS.md 记忆级联（docs 06 §4.1 / 03 §7）。

顺序：config/AGENTS.md → workspace/AGENTS.md → sessions/<sid>/AGENTS.md
一般 → 具体；缺层跳过；禁止环。文件每回合重读，手工编辑下一回合即生效。
"""

from __future__ import annotations

import os
from pathlib import Path


def cascade_paths(root: Path, session_id: str, workspace_dirs: list[Path] | None = None) -> list[Path]:
    """一般 → 具体：用户级、工作区级（default→active）、会话级；按物理路径去重。"""
    root = Path(root)
    workspace_paths = (
        [Path(directory) / "AGENTS.md" for directory in workspace_dirs]
        if workspace_dirs is not None
        else [root / "workspace" / "AGENTS.md"]
    )
    candidates = [root / "config" / "AGENTS.md", *workspace_paths,
                  root / "sessions" / session_id / "AGENTS.md"]
    seen: set[str] = set()
    result: list[Path] = []
    for path in candidates:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def read_cascade(root: Path, session_id: str, workspace_dirs: list[Path] | None = None) -> str:
    """返回按级联顺序拼接的记忆文本；空层跳过。"""
    parts: list[str] = []
    for path in cascade_paths(root, session_id, workspace_dirs):
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    parts.append(text)
        except OSError:
            continue
    return "\n\n".join(parts)
