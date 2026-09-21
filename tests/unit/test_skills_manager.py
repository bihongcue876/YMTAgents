"""SkillManager 测试（v0.0.4 / spec §2.3）：调和、注册、导入/更新/删除、权限覆盖、预置。"""

from __future__ import annotations

import json

import pytest

from core.registry.registry import Registry
from core.skills.manager import SkillManager
from core.store.config_store import ConfigStore

SKILL_TEXT = """---
name: 测试技能
description: 用于测试的一句话。
version: 1
permission: safe
---
原始正文
"""


def _make_skill_dir(root, sid: str, text: str = SKILL_TEXT, resources: bool = False):
    d = root / sid
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    if resources:
        (d / "resources").mkdir()
        (d / "resources" / "list.txt").write_text("r1", encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path):
    store = ConfigStore(tmp_path)
    registry = Registry()
    manager = SkillManager(store, registry, tmp_path / "skills")
    return store, registry, manager, tmp_path


def test_preset_installed_not_enabled(env, tmp_path):
    store, registry, _manager, _root = env
    presets = tmp_path / "presets"
    _make_skill_dir(presets, "skl_ymt_demo")
    manager = SkillManager(store, registry, tmp_path / "skills", presets_dir=presets)
    manager.reload()
    status = {s["id"]: s for s in manager.list_status()}
    assert status["skl_ymt_demo"]["builtin"] is True
    assert status["skl_ymt_demo"]["enabled"] is False
    assert (tmp_path / "skills" / "skl_ymt_demo" / "SKILL.md").is_file()
    assert registry.snapshot() == []  # 未启用不注册


def test_toggle_registers_and_unregisters(env):
    store, registry, manager, _root = env
    manager.reload()
    _make_skill_dir(manager.skills_root, "skl_x")
    manager.reload()
    manager.toggle("skl_x", True)
    names = [s.name for s in registry.snapshot()]
    assert "skill.skl_x" in names
    enabled = store.load("plugins").skills.enabled
    assert enabled == ["skl_x"]
    manager.toggle("skl_x", False)
    assert [s.name for s in registry.snapshot()] == []


def test_handler_returns_body_and_permission_override(env):
    store, registry, manager, _root = env
    manager.reload()
    _make_skill_dir(manager.skills_root, "skl_y")
    manager.reload()
    manager.toggle("skl_y", True)
    manager.set_permission("skl_y", "confirm")
    spec = next(s for s in registry.snapshot() if s.name == "skill.skl_y")
    assert spec.permission.value == "confirm"  # 覆盖 ⊕ 缺省（docs 09 §2）
    result = registry.execute("skill.skl_y", {})
    assert result.ok is True
    assert result.output == "原始正文"


def test_import_from_dir_with_resources_and_update(env):
    store, registry, manager, tmp_path = env
    manager.reload()
    src = _make_skill_dir(tmp_path / "src", "anyskill", resources=True)
    imported = manager.import_skill(str(src))
    assert len(imported) == 1
    sid = imported[0].id
    assert sid.startswith("skl_") and sid != "anyskill"  # 导入生成新 id
    origin = json.loads((manager.skills_root / sid / "skill.origin.json").read_text(encoding="utf-8"))
    assert origin["type"] == "dir" and origin["subpath"] == ""  # 根目录即技能目录
    assert (manager.skills_root / sid / "resources" / "list.txt").read_text(encoding="utf-8") == "r1"
    assert sid in store.load("plugins").skills.installed

    # 更新：改源文件后 update，正文随之变化，id 与启用态保持
    manager.toggle(sid, True)
    (src / "SKILL.md").write_text(
        SKILL_TEXT.replace("原始正文", "更新后正文").replace("name: 测试技能", "name: 改名技能"),
        encoding="utf-8",
    )
    meta = manager.update_skill(sid)
    assert meta.name == "改名技能"
    assert registry.execute(f"skill.{sid}", {}).output == "更新后正文"
    assert sid in store.load("plugins").skills.enabled


def test_import_multi_skill_repo(env, tmp_path):
    _store, _registry, manager, tmp_path = env
    manager.reload()
    repo = tmp_path / "repo"
    _make_skill_dir(repo, "skills-a")
    _make_skill_dir(repo, "skills-b")
    imported = manager.import_skill(str(repo))
    assert {m.name for m in imported} == {"测试技能", "测试技能"}
    assert len(imported) == 2
    assert len(manager.list_status()) >= 2


def test_import_rejects_empty_and_invalid(env, tmp_path):
    _store, _registry, manager, tmp_path = env
    manager.reload()
    with pytest.raises(ValueError):
        manager.import_skill("")
    with pytest.raises(ValueError):
        manager.import_skill(str(tmp_path / "nowhere"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        manager.import_skill(str(empty))  # 无任何 SKILL.md


def test_broken_skill_not_installed_but_listed(env):
    store, _registry, manager, _root = env
    manager.reload()
    broken = manager.skills_root / "skl_bad"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")
    manager.reload()
    assert "skl_bad" not in store.load("plugins").skills.installed
    status = {s["id"]: s for s in manager.list_status()}
    assert status["skl_bad"]["error"]  # 技能页可见异常原因


def test_delete_removes_and_protects_preset(env, tmp_path):
    store, registry, _manager, _root = env
    presets = tmp_path / "presets"
    _make_skill_dir(presets, "skl_ymt_demo")
    manager = SkillManager(store, registry, tmp_path / "skills", presets_dir=presets)
    manager.reload()
    _make_skill_dir(manager.skills_root, "skl_z")
    manager.reload()
    manager.toggle("skl_z", True)
    manager.delete("skl_z")
    assert not (manager.skills_root / "skl_z").exists()
    assert store.load("plugins").skills.installed == ["skl_ymt_demo"]
    assert [s.name for s in registry.snapshot()] == []
    with pytest.raises(ValueError):
        manager.delete("skl_ymt_demo")  # 预置不可删
