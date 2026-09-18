"""PersonaStore 单元测试（阶段 2 · spec rev23）。"""

from __future__ import annotations

import pytest

from core.agent.persona import PersonaStore, YMT_PERSONA_ID, YMT_PROMPT


@pytest.fixture()
def store(tmp_path):
    return PersonaStore(tmp_path / "personas")


def test_preset_exists_and_undeletable(store):
    """YMT 预置角色：缺席即创建、可编辑内容、**不可删除**。"""
    infos = store.list()
    assert infos and infos[0].id == YMT_PERSONA_ID
    assert infos[0].builtin and infos[0].name
    assert store.delete(YMT_PERSONA_ID) is False
    assert store.get(YMT_PERSONA_ID) is not None
    assert "言明通" in store.resolve_content(YMT_PERSONA_ID)


def test_save_list_and_default_roundtrip(store):
    pid = store.save(None, "评审员", "你是评审员。")
    assert pid != YMT_PERSONA_ID
    assert [p.id for p in store.list()] == [YMT_PERSONA_ID, pid]

    # 默认角色：初始 YMT → 指定后切换；缺省读取随清单
    assert store.current_default() == YMT_PERSONA_ID
    store.set_current_default(pid)
    assert store.current_default() == pid

    # 更新：同 id 再 save 不新增
    store.save(pid, "评审员（改）", "你是严格的评审员。")
    assert len(store.list()) == 2
    assert store.get(pid).name == "评审员（改）"
    assert store.get(pid).prompt == "你是严格的评审员。"


def test_resolve_content_falls_back_to_ymt(store):
    """会话引用的角色不存在/为 None → 回退 YMT 预置（角色体系永不空转）。"""
    assert store.resolve_content(None) == YMT_PROMPT
    assert store.resolve_content("prs_不存在") == YMT_PROMPT
    pid = store.save(None, "X", "专属提示词")
    assert store.resolve_content(pid) == "专属提示词"


def test_delete_removes_dir_and_resets_default(store):
    pid = store.save(None, "临时角色", "x")
    store.set_current_default(pid)
    assert store.delete(pid) is True
    assert store.get(pid) is None
    assert store.current_default() == YMT_PERSONA_ID  # 默认角色被删 → 回退 YMT
    assert store.delete(pid) is False  # 重复删除：False 而非报错


def test_preset_survives_reopen(tmp_path):
    """重开存储：预置不重建（用户可能已编辑），清单缺失时回缺省。"""
    root = tmp_path / "personas"
    s1 = PersonaStore(root)
    s1.save(None, "A", "a")
    s2 = PersonaStore(root)
    assert {p.id for p in s2.list()} >= {YMT_PERSONA_ID, s2.list()[-1].id}
    assert s2.get(YMT_PERSONA_ID).prompt == YMT_PROMPT
