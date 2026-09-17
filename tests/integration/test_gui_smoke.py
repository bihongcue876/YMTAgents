"""GUI 冒烟测试（离屏，无 WebEngine）。"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel

from app import bootstrap as bootstrap_mod
from app import paths
from gui import theme
from gui.main_window import MainWindow
from gui.sidebar import PANEL_MAX_PX, PANEL_MIN_PX, RAIL_PX
from gui.widgets.render.view import RendererView
from shared.envelope import (
    ModelSpec,
    NewSession,
    ProviderSpec,
    ProviderUpsert,
    ResumeSession,
    SendMessage,
    SettingsUpdate,
    SwitchModel,
)
from tests.mocks.gateway import MockGateway


def _window(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    return ctx, MainWindow(ctx.bridge, data_root=str(ctx.root))


def test_model_dropdown_follows_session_then_last_used(tmp_path, monkeypatch, qapp):
    """回归锚点（rev14）：模型下拉显示**当前会话**的选择；新对话回落「上次使用」。

    此前下拉恒显 slots.main：恢复一个换过模型的会话，头条显示的却是全局值 —— 说谎。
    """
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        qapp.processEvents()
        # 给 mock 端点补两个模型，供切换区分
        ctx.controller.handle(
            ProviderUpsert(
                provider=ProviderSpec(
                    id="prv_mock",
                    name="Mock",
                    base_url="https://mock.local",
                    models=[
                        ModelSpec(id="mock-model", ctx_window=8192),
                        ModelSpec(id="m2", ctx_window=8192),
                        ModelSpec(id="m3", ctx_window=8192),
                    ],
                ),
                api_key=None,
            )
        )
        qapp.processEvents()

        combo = window.chat.header._model
        assert combo.currentData() == "mock-model"

        # 会话 A 切到 m2（会话覆盖 + 上次使用）
        ctx.controller.handle(NewSession(title="A"))
        qapp.processEvents()
        sid_a = ctx.controller.current_session_id
        ctx.controller.handle(SwitchModel(slot="main", model_id="m2"))
        qapp.processEvents()
        assert combo.currentData() == "m2"

        # 新会话 B：无自身选择 → 回落「上次使用」= m2（而不是最初的 mock-model）
        ctx.controller.handle(NewSession(title="B"))
        qapp.processEvents()
        assert combo.currentData() == "m2"

        # B 切到 m3；恢复 A → 下拉显示 A 自己的 m2（不被全局 m3 冲掉）
        ctx.controller.handle(SwitchModel(slot="main", model_id="m3"))
        qapp.processEvents()
        assert combo.currentData() == "m3"
        ctx.controller.handle(ResumeSession(session_id=sid_a))
        qapp.processEvents()
        assert combo.currentData() == "m2"
    finally:
        window.close()


def test_renderer_textbrowser_opens_links_externally(monkeypatch, qapp):
    """回归锚点（rev15）：消息里的链接不得在应用内导航 —— 一律系统浏览器。

    在应用内打开外部网站 = 一个链接就能用钓鱼页顶掉整个对话流。
    """
    from PySide6.QtCore import QUrl

    opened: list[str] = []
    monkeypatch.setattr(
        "gui.widgets.render.view._open_external",
        lambda url: opened.append(url.toString()),
    )
    view = RendererView()  # 测试环境无 WebEngine → QTextBrowser 路径
    assert not view.using_webengine
    assert not view._view.openLinks(), "QTextBrowser 不得自行导航"
    view._view.anchorClicked.emit(QUrl("https://example.com/page"))
    assert opened == ["https://example.com/page"]


def test_theme_token_labels_fit_real_font(tmp_path, monkeypatch, qapp):
    """回归锚点（rev16）：主题字号 token 渲染的标签不得被**纵向**裁切。

    样式表 font-size 不参与 sizeHint —— rev11 修了横向（标题省略号），
    rev16 补纵向（截图实证：空状态两行文案同时被腰斩）。
    所有 token 标签在档位变化后统一重算最小高。
    """
    from gui.widgets import text_fit

    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        qapp.processEvents()
        pairs = [
            (window.chat.empty._title, "title"),
            (window.chat.empty._tagline, "ui"),
            (window.chat.empty._hint, "ui"),
            (window.models._title, "title"),
            (window.settings._title, "title"),
        ]
        for label, token in pairs:
            expected = text_fit.line_height(label, token, window._font_size)
            assert label.minimumHeight() >= expected, f"{label.objectName()} 纵向未适配"
        # rev16 系统性修法：应用字体 = ui 档 —— 默认字号标签（含换行说明）的 sizeHint 自准
        assert window.font().pixelSize() == theme.font_px("ui", window._font_size)
    finally:
        window.close()


def test_renderer_sets_theme_background(qapp):
    """回归锚点（rev16）：页面底色 = 主题背景 —— setHtml 重载瞬间不露白（暗色爆闪）。"""
    from gui.widgets.render.view import RendererView

    view = RendererView()  # 测试环境走 QTextBrowser 路径
    assert view._bg is None
    view.set_html("<html>x</html>", "#1E1F22")
    assert view._bg == "#1E1F22"
    assert "#1E1F22" in view._view.styleSheet()
    # 同色重复下发不重复设置；换色即更新
    view.set_html("<html>y</html>", "#1E1F22")
    view.set_html("<html>z</html>", "#26282C")
    assert view._bg == "#26282C"


def test_message_list_passes_theme_background(qapp):
    """消息流每次整帧渲染都随主题下发底色（爆闪修复的接线检查）。"""
    from gui.chat.message_list import MessageList

    ml = MessageList()
    ml.set_theme("dark")
    ml.add_user("hi")
    assert ml._renderer._bg == theme.palette("dark").bg


def test_sidebar_collapses_and_restores(tmp_path, monkeypatch, qapp):
    """回归锚点：侧栏折叠 = 面板藏起收成 rail 图标栏，展开回到上次宽度（rev13）。

    旧版 setFixedWidth(264) 不可折叠不可拖拽；折叠后入口（新对话/模型/设置）必须仍可点，
    故只藏会话面板、保留 rail。
    """
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        qapp.processEvents()

        assert window.sidebar.panel_visible() is True
        assert RAIL_PX + PANEL_MIN_PX <= window.sidebar.minimumWidth() <= RAIL_PX + PANEL_MAX_PX

        window.sidebar.toggle_requested.emit()
        qapp.processEvents()
        assert window.sidebar.panel_visible() is False
        assert window.sidebar.minimumWidth() == RAIL_PX

        # 展开：面板回来，宽度恢复到合法区间
        window.sidebar.toggle_requested.emit()
        qapp.processEvents()
        assert window.sidebar.panel_visible() is True
        assert window.sidebar.minimumWidth() >= RAIL_PX + PANEL_MIN_PX
    finally:
        window.close()


def test_main_window_min_width_fits_narrow_screens(tmp_path, monkeypatch, qapp):
    """回归锚点：主窗口最小宽必须放得进 125% 缩放的 1366 屏（逻辑宽 ~1093px）。

    旧实测：设置页两个长文本 QLabel 不换行，把整页最小宽撑到 710px、
    窗口最小宽近千 —— 修法 = 长标签 wordWrap + 侧栏可折叠。
    """
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        qapp.processEvents()
        assert window.minimumSizeHint().width() <= 1093
        # 元凶两个标签必须已开换行
        for name in ("dataPathLabel", "backupNote"):
            label = window.settings.findChild(QLabel, name)
            assert label is not None, name
            assert label.wordWrap(), f"{name} 必须开 wordWrap"
    finally:
        window.close()


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


def test_empty_state_title_width_follows_font_level(tmp_path, monkeypatch, qapp):
    """回归锚点：空状态标题「言明通 / YMTAgents」曾被裁掉（需 252px，实得 240px）。

    根因是样式表字号不参与 `sizeHint`；现在按档位重算最小宽度，且换档后跟着变。
    """
    from PySide6.QtGui import QFont, QFontMetrics

    from gui import theme

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    try:
        ctx.controller.push_initial_state()
        qapp.processEvents()
        title = window.chat.empty._title

        def needed(level: str) -> int:
            font = QFont(title.font())
            font.setPixelSize(theme.font_px("title", level))
            return QFontMetrics(font).horizontalAdvance(title.text())

        assert title.minimumWidth() >= needed(window._font_size)

        ctx.controller.handle(SettingsUpdate(section="ui", data={"font_size": "xlarge"}))
        qapp.processEvents()
        assert title.minimumWidth() >= needed("xlarge")
    finally:
        ctx.worker.stop()


def test_settings_context_labels_have_no_stray_characters(tmp_path, monkeypatch, qapp):
    """回归锚点：设置页标签曾写成「历史保留轮数 N」，界面直接显示多余的 " N"。"""
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    try:
        qapp.processEvents()
        texts = [label.text() for label in window.settings.findChildren(QLabel)]
        assert "历史保留轮数" in texts
        assert "输出预留（reserve）" in texts
        assert not any(t.endswith(" N") for t in texts)
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


def test_renderer_view_replays_hidden_updates(qapp):
    """回归锚点：WebEngine 对**不可见视图**的 setHtml 会被推迟或丢弃（spec rev12 §1）。

    用户在设置页切主题 → 对话页隐藏期间收到重渲染 → 切回后页面停留在旧外观甚至空白。
    修复：隐藏期置脏标记，showEvent 重放。本用例用降级路径（QTextBrowser）钉住该协议。
    """
    view = RendererView()
    calls: list[str] = []
    view._view.setHtml = lambda html: calls.append(html)  # type: ignore[method-assign]

    # 隐藏期更新：应记脏（不丢弃内容），真正 setHtml 至多一次（离屏下 isVisible 可能为 False）
    view.set_html("<html>first</html>")
    if not view.isVisible():
        assert view._dirty, "隐藏期更新必须置脏"
    shown_at = len(calls)

    # showEvent 重放：脏标记被消费后再次 setHtml
    view.show()
    qapp.processEvents()
    assert not view._dirty
    assert len(calls) > shown_at, "showEvent 必须重放隐藏期的更新"
    assert calls[-1] == "<html>first</html>"

    # 可见期间的更新直接生效、不置脏
    view.set_html("<html>second</html>")
    assert not view._dirty
    assert calls[-1] == "<html>second</html>"


# -- 空状态与模型导入（spec rev9 §2/§6） -------------------------------------


def test_empty_state_toggles_cta_and_switches_page(tmp_path, monkeypatch, qapp):
    """回归锚点：A1 要求对话视图空状态含「添加模型」CTA —— 此前全项目没有空状态实现。

    空状态与消息流互斥；CTA 随「有无供应商」切换，无供应商时点它跳模型配置页。
    """
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    list_providers = ctx.gateway.list_providers
    try:
        # 无供应商：CTA = 添加模型
        monkeypatch.setattr(ctx.gateway, "list_providers", lambda: [])
        ctx.controller.push_initial_state()
        qapp.processEvents()
        assert window.chat._stack.currentWidget() is window.chat.empty
        assert window.chat.empty.cta_text() == "添加模型"

        window.chat.empty._on_cta()
        qapp.processEvents()
        assert window.stack.currentWidget() is window.models

        # 有供应商：CTA = 开始对话；发消息后空状态让位给消息流
        monkeypatch.setattr(ctx.gateway, "list_providers", list_providers)
        ctx.controller.push_initial_state()
        qapp.processEvents()
        assert window.chat.empty.cta_text() == "开始对话"

        ctx.controller.handle(NewSession(title="空状态"))
        qapp.processEvents()
        assert window.chat._stack.currentWidget() is window.chat.empty

        ctx.controller.handle(SendMessage(text="hi"))
        qapp.processEvents()
        assert window.chat._stack.currentWidget() is window.chat.messages
    finally:
        ctx.worker.stop()


def test_models_page_reports_fetch_failure_in_chinese(tmp_path, monkeypatch, qapp):
    """端点不支持 /models 时给出可执行的中文指引，而不是裸错误码。"""
    from shared.envelope import ProviderModels

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    try:
        ctx.controller.push_initial_state()
        qapp.processEvents()
        provider_id = window.models._providers[0].id

        window.models.on_models_result(
            ProviderModels(provider_id=provider_id, ok=False, models=[], error="protocol_error")
        )
        label = window.models._fetch_labels[provider_id]
        assert "手动填写" in label.text()

        window.models.on_models_result(
            ProviderModels(provider_id=provider_id, ok=False, models=[], error="key_missing")
        )
        assert "凭据" in window.models._fetch_labels[provider_id].text()
    finally:
        ctx.worker.stop()


def test_models_page_fetch_ok_opens_picker(tmp_path, monkeypatch, qapp):
    """获取成功后弹勾选框；取消则不改配置（勾选结果经既有 upsert 落盘）。"""
    from PySide6.QtWidgets import QDialog

    from gui.pages import models as models_mod
    from shared.envelope import ProviderModels

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    seen: list = []
    try:
        ctx.controller.push_initial_state()
        qapp.processEvents()
        provider_id = window.models._providers[0].id
        monkeypatch.setattr(models_mod.ModelPickerDialog, "exec", lambda self: QDialog.Rejected)
        window.models.upsert_requested.connect(lambda spec, key: seen.append((spec, key)))

        window.models.on_models_result(
            ProviderModels(
                provider_id=provider_id, ok=True, models=["m-a", "m-b"], error=None
            )
        )
        assert "2" in window.models._fetch_labels[provider_id].text()
        assert seen == []  # 取消 → 不落盘
    finally:
        ctx.worker.stop()
