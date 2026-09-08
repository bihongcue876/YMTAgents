"""app.paths 单元测试（数据根解析与骨架生成）。"""

from __future__ import annotations

from app import paths


def test_data_root_name():
    assert paths.data_root().name == "ymtdata"
    assert not paths.is_frozen()


def test_ensure_skeleton(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    root = paths.ensure_skeleton()

    assert (root / "config" / "models.json").exists()
    assert (root / "config" / "settings.json").exists()
    assert (root / "sessions" / "index.json").exists()
    assert (root / "workspace" / "AGENTS.md").exists()
    assert (root / "workspace" / "files").is_dir()

    # 幂等：再次调用不报错
    paths.ensure_skeleton()
