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


def test_tmp_files_use_pid_suffix(tmp_path):
    """安全修订轮：固定「X.tmp」会与用户真实同名文件相撞——临时名必须带 pid。"""
    import os

    from core.store.atomic import atomic_write_text, _tmp_path

    target = tmp_path / "doc.txt"
    target.write_text("user real .tmp", encoding="utf-8")
    (tmp_path / "doc.txt.tmp").write_text("user real .tmp", encoding="utf-8")
    atomic_write_text(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"
    assert (tmp_path / "doc.txt.tmp").read_text(encoding="utf-8") == "user real .tmp"
    assert str(os.getpid()) in _tmp_path(target).name


def test_session_dir_rejects_malformed_session_id(tmp_path):
    """安全修订轮：会话标识直接拼路径——穿越 / 分隔符形态必须拒绝。"""
    from core.bus.sink import EventSink

    sink = EventSink(tmp_path)
    for bad in ("", ".", "..", "a/b", "a\\b", "x\x00y"):
        with pytest.raises(ValueError):
            sink.session_dir(bad)
    assert sink.session_dir("sea_ok").name == "sea_ok"


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


def test_config_store_memory_replaces_summary(tmp_path):
    """v0.0.1：全局配置项由 summary 改为 memory（MemoryConfig）。"""
    from shared.schema import CONFIG_FILES, MemoryConfig

    assert "memory" in CONFIG_FILES and "summary" not in CONFIG_FILES
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    assert store.path("memory").exists()
    config = store.load("memory")
    assert isinstance(config, MemoryConfig)
    assert config.use is True and config.compress is True


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


def test_atomic_write_retries_transient_lock(tmp_path, monkeypatch):
    """os.replace 撞 Windows 瞬时文件锁（WinError 5）时有界重试（rev55 实测的偶发）。"""
    import os as os_mod

    from core.store import atomic

    path = tmp_path / "a.json"
    real_replace = os_mod.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic.time, "sleep", lambda _s: None)
    atomic.atomic_write_json(path, {"x": 1})
    assert calls["n"] == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 1}


def test_atomic_write_raises_after_retries_exhausted(tmp_path, monkeypatch):
    """锁不释放：重试耗尽后照原样抛 —— fail-closed 不变，目标文件不出现半写。"""
    from core.store import atomic

    path = tmp_path / "a.json"
    calls = {"n": 0}

    def locked_replace(src, dst):
        calls["n"] += 1
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(atomic.os, "replace", locked_replace)
    monkeypatch.setattr(atomic.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        atomic.atomic_write_json(path, {"x": 1})
    assert calls["n"] == atomic._REPLACE_RETRIES + 1
    assert not path.exists()
