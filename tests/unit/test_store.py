"""core.store 单元测试（原子写 / 备份 / 单文件降级 / 版本门禁）。"""

from __future__ import annotations

import json

import pytest

from core.store import migrate
from core.store.atomic import atomic_write_json, backup_and_write_json
from core.store.config_store import ConfigStore
from shared.schema import ModelsConfig, SettingsConfig


def test_atomic_write_and_backup(tmp_path):
    path = tmp_path / "a.json"
    atomic_write_json(path, {"x": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 1}

    backup_and_write_json(path, {"x": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 2}
    bak = path.with_suffix(".json.bak")
    assert bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8")) == {"x": 1}


def test_config_store_defaults_and_degrade(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    assert store.path("models").exists()
    assert isinstance(store.load("models"), ModelsConfig)

    # 单文件损坏 -> 回退默认，不抛异常
    store.path("settings").write_text("{ not json", encoding="utf-8")
    assert isinstance(store.load("settings"), SettingsConfig)


def test_config_store_roundtrip(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    cfg = store.load("settings")
    cfg.ui.theme = "dark"
    cfg.logging.level = "DEBUG"
    store.save("settings", cfg)
    loaded = store.load("settings")
    assert loaded.ui.theme == "dark"
    assert loaded.logging.level == "DEBUG"
    assert not hasattr(loaded, "context"), "rev24：全局上下文设置已移除"


def test_migrate_guard(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    migrate.migrate_all(tmp_path)  # 版本 1，正常通过

    path = store.path("models")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(migrate.SchemaTooNewError):
        migrate.migrate_all(tmp_path)
