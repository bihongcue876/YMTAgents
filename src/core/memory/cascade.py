"""AGENTS.md 记忆级联（docs 06 §4.1 / 03 §7）。

顺序：config/AGENTS.md → workspace/AGENTS.md → sessions/<sid>/AGENTS.md
一般 → 具体；缺层跳过；禁止环。文件每回合重读，手工编辑下一回合即生效。
"""

from __future__ import annotations

from pathlib import Path


def cascade_paths(root: Path, session_id: str) -> list[Path]:
    return [
        root / "config" / "AGENTS.md",
        root / "workspace" / "AGENTS.md",
        root / "sessions" / session_id / "AGENTS.md",
    ]


def read_cascade(root: Path, session_id: str) -> str:
    """返回按级联顺序拼接的记忆文本；空层跳过。"""
    parts: list[str] = []
    for path in cascade_paths(root, session_id):
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    parts.append(text)
        except OSError:
            continue
    return "\n\n".join(parts)
