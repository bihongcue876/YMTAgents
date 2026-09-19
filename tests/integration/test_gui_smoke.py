"""GUI 冒烟测试（离屏，无 WebEngine）。"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel

from app import bootstrap as bootstrap_mod
from app import paths
from gui import theme
from gui.main_window import MainWindow
from gui.sidebar import PANEL_MAX_PX, PANEL_MIN_PX, RAIL_BTN_W, RAIL_PX
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


def test_model_dropdown_follows_session_then_global_default(tmp_path, monkeypatch, qapp):
    """回归锚点（rev14/rev23 修订）：下拉显示**当前会话**的选择；新对话用全局默认。

    rev23 语义（对齐 Coding agents 平台）：对话内切换只属于该对话，不再登记「上次使用」
    —— 新会话 B 用的是全局默认 mock-model，而恢复会话 A 仍显示 A 自己的 m2。
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

        # 会话 A 切到 m2（会话级选择）
        ctx.controller.handle(NewSession(title="A"))
        qapp.processEvents()
        sid_a = ctx.controller.current_session_id
        ctx.controller.handle(SwitchModel(slot="main", model_id="m2"))
        qapp.processEvents()
        assert combo.currentData() == "m2"

        # 新会话 B：全局默认未被 A 的会话内选择漂移 → 仍是 mock-model
        ctx.controller.handle(NewSession(title="B"))
        qapp.processEvents()
        assert combo.currentData() == "mock-model"

        # 恢复 A → 下拉显示 A 自己的 m2（会话级选择不丢）
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
    view.set_stream("<p>hi</p>")  # rev19 起视图惰性创建：先渲染才有视图
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


def test_message_list_passes_theme_background(qapp):
    """消息流每次整帧渲染都随主题下发底色（rev16 爆闪修复的接线检查，rev19 起走片段接口）。"""
    from gui.chat.message_list import MessageList

    ml = MessageList()
    assert ml._renderer._view is None  # 惰性：无消息不建视图
    ml.set_theme("dark")
    ml.add_user("hi")
    assert ml._renderer._bg == theme.palette("dark").bg
    assert ml._renderer._view is not None  # 首帧后视图已建


def test_input_bar_height_two_to_ten_lines(qapp):
    """rev25（用户裁决）：输入区默认 2 行，随输入最多长到 10 行，发送清空后回到 2 行。"""
    from gui.chat.input_bar import _LINE_PX, _MAX_LINES, _MIN_LINES, InputBar

    assert (_MIN_LINES, _MAX_LINES) == (2, 10)
    bar = InputBar()
    assert bar._edit.height() == _MIN_LINES * _LINE_PX + 10
    bar._edit.setPlainText("\n".join(str(i) for i in range(20)))
    assert bar._edit.height() == _MAX_LINES * _LINE_PX + 10
    bar._edit.clear()
    assert bar._edit.height() == _MIN_LINES * _LINE_PX + 10


def test_usage_text_includes_generation_tps(qapp):
    """rev25：用量文案含「本次」口径 + 生成阶段 TPS + 总耗时；缺计时则不编速度。"""
    from gui.chat.message_list import MessageList

    text = MessageList.format_usage(
        {"total_tokens": 30, "completion_tokens": 20, "elapsed_ms": 1200, "first_token_ms": 200}
    )
    assert "本次 30 tokens" in text
    assert "20.0 tokens/s" in text  # 20 tokens ÷ 1.0s（生成阶段 = 1200-200ms）
    assert "1.2s" in text
    assert MessageList.format_usage(None) is None
    assert MessageList.format_usage({"total_tokens": 5}) == "本次 5 tokens"


def test_rail_buttons_carry_text_and_default_size_is_large(tmp_path, monkeypatch, qapp):
    """回归锚点（rev18）：rail 按钮图标右侧带文字；启动尺寸比旧的 1100×720 大。

    只看图标猜不出功能（用户反馈）；启动尺寸在高分屏上应占可用区域的大部分（上限 1440×920）。
    """
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        for btn, word in (
            (window.sidebar._toggle, "侧栏"),
            (window.sidebar._models_btn, "模型"),
            (window.sidebar._settings_btn, "设置"),
        ):
            assert word in btn.text(), f"rail 按钮缺文字：{btn.text()!r}"
            assert btn.width() == RAIL_BTN_W
        w, h = window._default_size()
        assert w >= 1024 and h >= 720
        assert w <= 1440 and h <= 920
    finally:
        window.close()


def test_persona_ui_wiring(tmp_path, monkeypatch, qapp):
    """阶段 2 第一片（rev23）：角色页 + 头条角色下拉端到端接通。"""
    import time

    from PySide6.QtWidgets import QPushButton

    def wait_for(qapp, cond, timeout_s: float = 5.0) -> bool:
        """经 worker 线程的请求是异步的：轮询事件循环直至条件成立。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            qapp.processEvents()
            if cond():
                return True
            time.sleep(0.02)
        return False

    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        qapp.processEvents()
        # rail 有「角色」入口，角色页已在 stack
        assert any("角色" in b.text() for b in window.sidebar.findChildren(QPushButton))
        assert window.stack.widget(2) is window.personas_page
        # persona.list 已推送：预置角色在下拉里
        assert window._personas_cache and any(
            p.id == "prs_ymt" and p.builtin for p in window._personas_cache
        )
        combo = window.chat.header._persona
        assert combo.count() >= 1 and combo.itemData(0) == "prs_ymt"

        # 新建角色（页面请求，经 worker 线程异步）→ 列表刷新出现
        window.personas_page.save_requested.emit(None, "评审员", "你是评审员。")
        appeared = wait_for(
            qapp, lambda: any(p.name == "评审员" for p in window._personas_cache)
        )
        assert appeared, "保存角色后 persona.list 必须刷新到界面"
        pid = next(p.id for p in window._personas_cache if p.name == "评审员")

        # 会话切换角色 → 下拉跟随（本例无会话 → 落到全局默认）
        window.chat.switch_persona.emit(pid)
        switched = wait_for(qapp, lambda: window._default_persona == pid)
        assert switched
        assert combo.currentData() == pid
    finally:
        window.close()


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


def test_empty_state_short_lines_do_not_wrap(qapp):
    """rev29：空状态两行短句各占一行（此前居中 alignment 让标签只得窄宽，短句被折成两行）。"""
    from gui.chat.empty_state import EmptyState
    from gui.widgets import text_fit

    widget = EmptyState()
    widget.set_has_provider(True)  # 取短句 HINT_READY
    widget.resize(900, 500)
    widget.show()
    qapp.processEvents()
    widget.refresh_metrics("normal")
    qapp.processEvents()
    try:
        single = text_fit.line_height(widget._tagline, "ui", "normal")
        assert widget._tagline.minimumHeight() == single  # 一行
        assert widget._hint.minimumHeight() == single
    finally:
        widget.close()


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


def test_settings_context_section_removed(tmp_path, monkeypatch, qapp):
    """回归锚点（rev24）：全局「上下文策略」设置段已移除，改为逐会话在右侧详情面板设置。

    旧设置页标签「历史保留轮数 / 输出预留（reserve）」随全局上下文设置一并下线；
    界面标签也不得残留 " N" 这类占位符。
    """
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    try:
        qapp.processEvents()
        texts = [label.text() for label in window.settings.findChildren(QLabel)]
        assert "历史保留轮数" not in texts
        assert "输出预留（reserve）" not in texts
        assert not any(t.endswith(" N") for t in texts)
    finally:
        ctx.worker.stop()


def test_renderer_view_forwards_font_size(monkeypatch, qapp):
    """回归锚点：`RendererView.set_markdown` 必须透传字号档位。

    此前该入口只透传主题，调用方即便拿到用户字号也无处可传（rev7 遗留缺口）。
    """
    captured: dict = {}

    def fake_inner(text, theme=None, font_size=None):
        captured["args"] = (text, theme, font_size)
        return "<p>hi</p>"

    monkeypatch.setattr("gui.widgets.render.view.markdown_inner", fake_inner)
    view = RendererView()
    view.set_markdown("hi", "dark", "xlarge")
    assert captured["args"] == ("hi", "dark", "xlarge")


def test_renderer_view_is_lazy_and_uses_stub_plus_js(qapp):
    """回归锚点（rev19）：视图惰性创建（启动提速）+ 壳只加载一次 + 后续帧走 JS 更新。

    用降级路径（QTextBrowser）钉住协议：未渲染前不建视图；首帧后视图存在。
    JS 路径的脚本正确性另由 `_update_script` 单测覆盖，真实 WebEngine 由联网自检覆盖。
    """
    view = RendererView()
    assert view._view is None, "未渲染前不得创建视图（WebEngine 拉起渲染进程是启动慢的大头）"
    view.set_stream("<p>first</p>", "#FFFFFF")
    assert view._view is not None
    assert view._bg == "#FFFFFF"
    # 同色重复下发不重复设置；换色即更新
    view.set_stream("<p>second</p>", "#FFFFFF")
    view.set_stream("<p>third</p>", "#26282C")
    assert view._bg == "#26282C"


def test_renderer_view_replays_hidden_updates(qapp):
    """回归锚点：WebEngine 对**不可见视图**的 setHtml 会被推迟或丢弃（spec rev12 §1）。

    用户在设置页切主题 → 对话页隐藏期间收到重渲染 → 切回后页面停留在旧外观甚至空白。
    修复：隐藏期置脏标记，showEvent 重放。本用例用降级路径（QTextBrowser）钉住该协议。
    """
    view = RendererView()
    view.set_stream("<p>first</p>")  # 先建视图（隐藏状态下）
    calls: list[str] = []
    view._view.setHtml = lambda html: calls.append(html)  # type: ignore[method-assign]
    view._inner = ""

    # 隐藏期更新：应记脏（不丢弃内容），真正 setHtml 至多一次（离屏下 isVisible 可能为 False）
    view.set_stream("<p>first</p>")
    if not view.isVisible():
        assert view._dirty, "隐藏期更新必须置脏"
    shown_at = len(calls)

    # showEvent 重放：脏标记被消费后再次 setHtml
    view.show()
    qapp.processEvents()
    assert not view._dirty
    assert len(calls) > shown_at, "showEvent 必须重放隐藏期的更新"
    assert "first" in calls[-1]

    # 可见期间的更新直接生效、不置脏
    view.set_stream("<p>second</p>")
    assert not view._dirty
    assert "second" in calls[-1]


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


def test_session_panel_summary_controls(qapp):
    """右栏压缩控件（rev26）：阈值可指定、按钮触发、保存随会话、失败有提示。"""
    from datetime import datetime, timezone

    from gui.chat.session_panel import SessionPanel
    from shared.envelope import SessionDetailResult, SessionMeta

    panel = SessionPanel()
    triggered: list = []
    panel.summarize_requested.connect(lambda: triggered.append(True))
    panel._summarize.click()
    assert triggered == [True]

    saved: list = []
    panel.save_requested.connect(saved.append)
    now = datetime.now(timezone.utc)
    meta = SessionMeta(id="s", title="T", created_at=now, updated_at=now)
    panel.set_detail(
        SessionDetailResult(
            session_id="s",
            meta=meta,
            summary_revision=0,
            summary_covered_seq=-1,
            summary_tokens=0,
            summary_threshold=90,
        ),
        ["问题一"],
    )
    assert "尚未压缩" in panel._summary_state.text()
    assert panel._threshold_auto.isChecked()
    panel._on_save()
    assert saved and saved[-1]["summary_threshold"] is None  # 跟随默认

    meta2 = meta.model_copy(update={"summary_threshold": 75})
    panel.set_detail(
        SessionDetailResult(
            session_id="s",
            meta=meta2,
            summary_revision=2,
            summary_covered_seq=5,
            summary_tokens=120,
            summary_threshold=75,
        ),
        [],
    )
    assert "已压缩 rev 2" in panel._summary_state.text()
    assert not panel._threshold_auto.isChecked()
    assert panel._threshold.value() == 75
    panel._on_save()
    assert saved[-1]["summary_threshold"] == 75  # 用户指定值随会话提交

    panel.set_summary_error("上游拒绝")
    assert "压缩未完成" in panel._summary_state.text()


def test_session_panel_question_list_features(qapp):
    """问题列表（rev30）：轮次编号、当前高亮、完整内容、搜索过滤与折叠。"""
    from datetime import datetime, timezone

    from PySide6.QtCore import Qt

    from gui.chat.session_panel import SessionPanel
    from shared.envelope import SessionDetailResult, SessionMeta

    panel = SessionPanel()
    panel.show()
    now = datetime.now(timezone.utc)
    meta = SessionMeta(id="s", title="T", created_at=now, updated_at=now)
    long_question = "这是一个很长的提问" * 20
    questions = ["第一个问题", long_question, "关于上下文的提问"]
    panel.set_detail(
        SessionDetailResult(session_id="s", meta=meta, summary_threshold=90),
        questions,
    )

    assert panel._questions.count() == 3
    assert panel._questions.item(0).text().startswith("第 1 轮　第一个问题")
    assert long_question in panel._questions.item(1).text()  # 完整内容：不截断
    assert not panel._questions.item(0).font().bold()
    assert panel._questions.item(2).font().bold()  # 末条 = 当前所在位置
    assert "当前所在位置" in panel._questions.item(2).toolTip()
    assert "共 3 条" in panel._question_count.text()

    selected: list[int] = []
    panel.question_selected.connect(selected.append)
    panel._question_search.setText("上下文")  # 过滤后仍按原始序号跳转
    assert panel._questions.count() == 1
    assert "筛出 1 条" in panel._question_count.text()
    item = panel._questions.item(0)
    assert item.data(Qt.UserRole) == 2
    panel._questions.itemClicked.emit(item)
    assert selected == [2]

    panel._question_toggle.setChecked(False)  # 折叠
    assert not panel._question_body.isVisible()
    assert panel._question_toggle.text() == "展开"


def test_session_panel_branch_section(qapp):
    """分支段（rev31）：列表文案/当前高亮/切换信号，以及问题项右键两个动作信号。"""
    from datetime import datetime, timezone

    from gui.chat.session_panel import SessionPanel
    from shared.envelope import BranchInfo, SessionBranches

    panel = SessionPanel()
    panel.show()
    now = datetime.now(timezone.utc)
    event = SessionBranches(
        session_id="s",
        active="br1",
        max_branches=5,
        branches=[
            BranchInfo(id="br0", parent=None, fork_seq=-1, created_at=now, turns=3, head_seq=6),
            BranchInfo(
                id="br1", parent="br0", fork_seq=3, created_at=now, turns=2, head_seq=5,
                active=True,
            ),
        ],
    )
    panel.set_branches(event)
    assert panel._branch_list.count() == 2
    assert "主干 br0" in panel._branch_list.item(0).text()
    assert not panel._branch_list.item(0).font().bold()
    assert panel._branch_list.item(1).font().bold()
    assert "分叉于 #3" in panel._branch_list.item(1).text()
    assert "（当前）" in panel._branch_list.item(1).text()
    assert "共 2/5 分支" in panel._branch_state.text()

    switched: list[str] = []
    panel.branch_switch_requested.connect(switched.append)
    panel._branch_list.itemClicked.emit(panel._branch_list.item(0))
    assert switched == ["br0"]

    reverts: list[int] = []
    branches: list[int] = []
    panel.revert_requested.connect(reverts.append)
    panel.branch_requested.connect(branches.append)
    panel.revert_requested.emit(1)  # 菜单动作语义即这两个信号
    panel.branch_requested.emit(2)
    assert reverts == [1] and branches == [2]

    panel.clear()
    assert panel._branch_list.count() == 0
    assert "共 1/5 分支" in panel._branch_state.text()
