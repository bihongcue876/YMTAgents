"""Skills 端到端集成（v0.0.4 / spec §1.4 验收锚点）：controller 入口 → 注册表 → 正文回注 → 回放。"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from core.registry.executor import ToolContext
from shared.envelope import (
    NewSession,
    SendMessage,
    SkillDelete,
    SkillImport,
    SkillPermission,
    SkillToggle,
    SkillsRefresh,
)
from tests.mocks.gateway import MockGateway


def _boot(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=lambda _store: MockGateway())


def _collect(ctx):
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    return events


def _skill_tools(ctx) -> list[str]:
    """当前下发工具里的技能工具名（技能面的断言不受其它工具载体的影响）。"""
    return [
        p["function"]["name"]
        for p in ctx.executor.tool_payloads()
        if p["function"]["name"].startswith("skill.")
    ]


SKILL_TEXT = """---
name: 集成技能
description: 集成测试用技能。
version: 1
permission: safe
---
集成正文标记
"""


def _make_source(tmp_path):
    src = tmp_path / "skill-src"
    src.mkdir(exist_ok=True)
    (src / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    return src


def test_presets_pushed_and_registry_empty_on_boot(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        ctx.controller.push_initial_state()
        skill_list = [e for e in events if e.type == "skill.list"][0]
        ids = [s["id"] for s in skill_list.skills]
        assert ids == ["skl_ymt_plan", "skl_ymt_review"]  # 预置可选：安装、未启用
        assert all(s["builtin"] and not s["enabled"] for s in skill_list.skills)
        # 未启用 → 不下发该技能的工具。注意：断言的是**没有 skill.\* 工具**，
        # 不是「注册表为空」—— v0.0.5 起 shell.exec 默认可用（本机有解释器时）。
        assert _skill_tools(ctx) == []
    finally:
        ctx.worker.stop()


def test_full_skill_lifecycle(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        ctx.controller.push_initial_state()
        # 1. 导入（controller 入口）
        src = _make_source(tmp_path)
        ctx.controller.handle(SkillImport(source=str(src)))
        result = [e for e in events if e.type == "skill.import.result"][-1]
        assert result.ok is True
        sid = result.skill_ids[0]
        # 2. 启用 → 注册为工具
        ctx.controller.handle(SkillToggle(id=sid, enabled=True))
        names = [p["function"]["name"] for p in ctx.executor.tool_payloads()]
        assert f"skill.{sid}" in names
        # 3. safe 直行执行 → 正文全文回注（不外置），落盘 tool.call/tool.result
        ctx.controller.handle(NewSession())
        session_id = ctx.controller.current_session_id
        call = ctx.executor.execute(
            "call-1", f"skill.{sid}", {}, ToolContext(session_id=session_id)
        )
        assert call.ok is True and call.output == "集成正文标记"
        assert call.output_ref is None
        logged = [e["type"] for e in ctx.session_store.replay(session_id)]
        assert "tool.call" in logged and "tool.result" in logged
        # 4. 权限覆盖 → ToolSpec 档位随之变化
        ctx.controller.handle(SkillPermission(id=sid, permission="confirm"))
        spec = next(s for s in ctx.registry.snapshot() if s.name == f"skill.{sid}")
        assert spec.permission.value == "confirm"
        ctx.controller.handle(SkillPermission(id=sid, permission="safe"))
        # 5. 删除 → 注销 + 目录移除
        ctx.controller.handle(SkillDelete(id=sid))
        assert not (ctx.root / "skills" / sid).exists()
        assert _skill_tools(ctx) == []  # 该技能已下线（shell.exec 不属本测试关注面）
        # 6. 刷新 → 列表回发
        events.clear()
        ctx.controller.handle(SkillsRefresh())
        assert any(e.type == "skill.list" for e in events)
    finally:
        ctx.worker.stop()


def test_preset_delete_refused_and_send_turn_with_skill(tmp_path, monkeypatch, qapp):
    """预置不可删 + 启用预置后 SendMessage 正常走通（skill 工具随回合下发）。"""
    ctx = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        ctx.controller.push_initial_state()
        ctx.controller.handle(SkillDelete(id="skl_ymt_review"))
        error = [e for e in events if e.type == "error"][-1]
        assert error.code == "invalid_request"
        assert "不可删除" in error.message

        ctx.controller.handle(SkillToggle(id="skl_ymt_review", enabled=True))
        ctx.controller.handle(NewSession())
        events.clear()
        ctx.controller.handle(SendMessage(text="hi"))
        types = [e.type for e in events]
        assert "msg.assistant.final" in types  # 回合正常完成（Mock 网关不调工具）
        names = [p["function"]["name"] for p in ctx.executor.tool_payloads()]
        assert "skill.skl_ymt_review" in names
    finally:
        ctx.worker.stop()
