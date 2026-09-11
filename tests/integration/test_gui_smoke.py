"""GUI 冒烟测试（离屏，无 WebEngine）。"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from gui import theme
from gui.main_window import MainWindow
from shared.envelope import NewSession, SendMessage, SettingsUpdate
from tests.mocks.gateway import MockGateway


def test_main_window_dispatch(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    try:
        ctx.controller.push_initial_state()
        qapp.processEvents()

        # 首屏：无供应商时模型页提示
        assert window.sidebar is not None
        assert window.chat is not None

        # 新建会话 -> 侧栏出现，主区切到对话
        ctx.controller.handle(NewSession(title="GUI"))
        qapp.processEvents()
        assert window._current_session_id
        assert window.stack.currentWidget() is window.chat

        # 发送消息 -> 助手消息渲染
        ctx.controller.handle(SendMessage(text="hi"))
        qapp.processEvents()
        assert any(m["role"] == "assistant" for m in window.chat.messages._messages)

        # 主题切换：settings.update("ui") -> 全局 QSS 换肤 + 消息流重渲染
        try:
            ctx.controller.handle(SettingsUpdate(section="ui", data={"theme": "dark"}))
            qapp.processEvents()
            assert window._theme == "dark"
            assert theme.palette("dark").bg in (qapp.styleSheet() or "")
            assert window.chat.messages._theme == "dark"

            ctx.controller.handle(SettingsUpdate(section="ui", data={"theme": "light"}))
            qapp.processEvents()
            assert window._theme == "light"
        finally:
            ctx.controller.handle(SettingsUpdate(section="ui", data={"theme": "light"}))
    finally:
        ctx.worker.stop()
