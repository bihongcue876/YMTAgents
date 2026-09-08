"""Schema 版本检查与迁移（docs 03 §9）。

- 只进不退：迁移单向向新版本演进，不写降级逻辑。
- 版本高于程序已知 → 明确报错拒载（不静默降级）。
- 迁移前整体复制到 `ymtdata/backup/schema/<旧v>_<ts>/`。
- 首期当前版本为 1，无实际迁移步骤，框架到位。
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from shared.schema import CONFIG_FILES

log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1


class SchemaTooNewError(RuntimeError):
    """配置文件版本高于程序已知版本。"""


def read_version(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("schema_version")
    return version if isinstance(version, int) else None


def _backup(path: Path, version: int, backup_root: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_root / "schema" / f"v{version}_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest / path.name)
    log.warning("迁移前备份 %s -> %s", path, dest)


def ensure_migrated(path: Path, backup_root: Path) -> None:
    """检查单个文件版本；过高报错，过低先备份（首期无迁移步骤）。"""
    version = read_version(path)
    if version is None:
        return
    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"{path.name} schema_version={version} 高于程序已知 {CURRENT_SCHEMA_VERSION}"
        )
    if version < CURRENT_SCHEMA_VERSION:
        _backup(path, version, backup_root)
        # 首期无迁移步骤；未来在此按步迁移并原子写回。


def migrate_all(root: Path) -> None:
    """对 `ymtdata/config/` 下所有配置执行版本检查。"""
    config_dir = root / "config"
    for filename, _model in CONFIG_FILES.values():
        ensure_migrated(config_dir / filename, root / "backup")
