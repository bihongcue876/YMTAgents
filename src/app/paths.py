"""数据根路径解析与骨架生成（docs 02 §4 / 03 §2）。

- 开发期：数据根 = <项目根>/ymtdata
- 分发后：数据根 = <EXE 同级>/ymtdata（PyInstaller 冻结态）
所有数据内部路径一律相对数据根，物理定位只经本模块。
"""

from __future__ import annotations

import sys
from pathlib import Path

DATA_DIR_NAME = "ymtdata"


def project_root() -> Path:
    # src/app/paths.py -> parents[2] = 项目根
    return Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent / DATA_DIR_NAME
    return project_root() / DATA_DIR_NAME


def ensure_skeleton() -> Path:
    """创建 `ymtdata/` 目录树与缺失的默认文件（幂等）。"""
    from core.store.atomic import atomic_write_json, atomic_write_text
    from core.store.config_store import ConfigStore

    root = data_root()
    for rel in (
        "config",
        "config/personas",
        "sessions",
        "secrets",
        "libraries",
        "workspaces",  # v0.0.6：工作区登记表所在（index.json）
        "workspace",
        "workspace/files",
        # v0.0.6：默认工作区（ws_default）的 root 与 data_home **重合**于 workspace/，
        # 故其文档与产物目录就落在这里 —— 与 v0.0.5 及以前的形状完全一致。
        "workspace/documents",
        "workspace/artifacts",
        "skills",
        "backup",
        "logs",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)

    ConfigStore(root).ensure_defaults()

    index = root / "sessions" / "index.json"
    if not index.exists():
        atomic_write_json(index, [])

    manifest = root / "workspace" / "manifest.json"
    if not manifest.exists():
        atomic_write_json(manifest, {"schema_version": 1, "files": []})

    workspace_memory = root / "workspace" / "AGENTS.md"
    if not workspace_memory.exists():
        atomic_write_text(workspace_memory, "")

    return root
