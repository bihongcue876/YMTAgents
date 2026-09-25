"""会话工具白名单集成冒烟（rev68）。

从 `controller.handle(SessionToolset)` 入口：meta 落盘、session.toolset 事件落 events、
session.note 即时提示；执行器按白名单过滤（真实 ToolExecutor）。
"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import (
    NewSession,
    SendMessage,
    SessionNote,
    SessionToolset,
    ToolCatalog,
)
from tests.mocks.gateway import MockGateway


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())


def test_session_toolset_via_controller(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    try:
        events: list = []
        ctx.bridge.event_received.connect(events.append)

        ctx.controller.handle(NewSession())
        sid = ctx.controller.current_session_id
        ctx.controller.push_initial_state()  # 目录快照在首屏推送（tool.catalog）

        # 目录快照已推送（含注册的工具，如 file.*）
        catalogs = [e for e in events if isinstance(e, ToolCatalog)]
        assert catalogs and catalogs[-1].items, "工具目录应有内容"

        # 提交白名单：只留一个已知工具（从快照里取）
        known = catalogs[-1].items[0]["name"]
        ctx.controller.handle(SessionToolset(tools=[known]))

        # meta 落盘
        assert ctx.session_store.get_meta(sid).toolset == [known]

        # session.toolset 事件落盘（回放可见）+ session.note 即时提示
        replay = ctx.session_store.replay(sid)
        assert any(e["type"] == "session.toolset" for e in replay)
        assert any(isinstance(e, SessionNote) and "工具权限" in e.text for e in events)

        # 执行器按白名单过滤：名单外工具派发被拒（tool_denied）
        others = [i["name"] for i in catalogs[-1].items if i["name"] != known]
        if others:
            from core.registry.executor import ToolContext
            from core.registry.registry import ToolResult

            result = ctx.executor.execute("c1", others[0], {}, None)
            result2 = ctx.executor.execute(
                "c2", others[0], {},
                type("Ctx", (), {"session_id": sid})(),
            )
            assert result.ok is False and result.error["code"] == "tool_denied"
            _ = ToolResult

        # 恢复全部可用
        ctx.controller.handle(SessionToolset(tools=None))
        assert ctx.session_store.get_meta(sid).toolset is None
        notes = [e for e in events if isinstance(e, SessionNote)]
        assert notes and "全部工具可用" in notes[-1].text
    finally:
        ctx.worker.stop()
