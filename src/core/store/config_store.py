"""配置读写（docs 03 §3）。

职责：装载/保存 `ymtdata/config/` 下四个 JSON 配置。
- 保存：单代备份 + 原子写（docs 03 §10）。
- 装载：单文件损坏时回退默认值并告警，不整体拒启（docs 04 §6 阶段 3）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel

from shared.schema import CONFIG_FILES

from core.store.atomic import atomic_write_json, atomic_write_text, backup_and_write_json

log = logging.getLogger(__name__)

MEMORY_FILE = "AGENTS.md"


class ConfigStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.config_dir = self.root / "config"

    def path(self, kind: str) -> Path:
        filename, _ = CONFIG_FILES[kind]
        return self.config_dir / filename

    def load(self, kind: str) -> BaseModel:
        filename, model_cls = CONFIG_FILES[kind]
        path = self.config_dir / filename
        if not path.exists():
            return model_cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return model_cls.model_validate(data)
        except (OSError, ValueError) as exc:
            log.warning("配置 %s 损坏，回退默认值：%s", filename, exc)
            return model_cls()

    def save(self, kind: str, obj: BaseModel) -> None:
        backup_and_write_json(self.path(kind), obj.model_dump(mode="json"))

    def ensure_defaults(self) -> None:
        """创建缺失的配置文件与用户级记忆文件（不覆盖已存在的）。"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        for kind, (filename, model_cls) in CONFIG_FILES.items():
            path = self.config_dir / filename
            if not path.exists():
                atomic_write_json(path, model_cls().model_dump(mode="json"))
        memory = self.config_dir / MEMORY_FILE
        if not memory.exists():
            atomic_write_text(memory, "")
