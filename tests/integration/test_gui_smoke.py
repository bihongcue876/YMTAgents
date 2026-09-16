"""GUI 冒烟测试（离屏，无 WebEngine）。"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from gui import theme
from gui.main_window import MainWindow
from gui.widgets.render.view import RendererView
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


def test_main_window_applies_font_size(tmp_path, monkeypatch, qapp):
    """字号切换：settings.update("ui") -> 全局 QSS 重排 + 消息流整帧重渲染。"""
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    try:
        ctx.controller.push_initial_state()
        qapp.processEvents()
        assert window._font_size == theme.DEFAULT_FONT_SIZE

        ctx.controller.handle(SettingsUpdate(section="ui", data={"font_size": "xlarge"}))
        qapp.processEvents()
        assert window._font_size == "xlarge"
        assert f"font-size: {theme.font_px('ui', 'xlarge')}px" in (qapp.styleSheet() or "")
        assert window.chat.messages._font_size == "xlarge"

        # 回到标准档（默认），确认可逆
        ctx.controller.handle(SettingsUpdate(section="ui", data={"font_size": "normal"}))
        qapp.processEvents()
        assert window._font_size == "normal"
    finally:
        ctx.worker.stop()


def test_renderer_view_forwards_font_size(monkeypatch, qapp):
    """回归锚点：`RendererView.set_markdown` 必须透传字号档位。

    此前该入口只透传主题，调用方即便拿到用户字号也无处可传（rev7 遗留缺口）。
    """
    captured: dict = {}

    def fake_md(text, theme=None, font_size=None):
        captured["args"] = (text, theme, font_size)
        return "<html></html>"

    monkeypatch.setattr("gui.widgets.render.view.markdown_to_html", fake_md)
    view = RendererView()
    view.set_markdown("hi", "dark", "xlarge")
    assert captured["args"] == ("hi", "dark", "xlarge")
