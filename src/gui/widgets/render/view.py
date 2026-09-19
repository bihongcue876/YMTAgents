"""渲染视图：QWebEngineView 本地渲染 + QTextBrowser 降级（docs 05 §3）。

渲染策略（rev19，依据社区经验与 Qt 文档交叉验证）：
- WebEngine 首次渲染只加载一次**空壳文档**；此后所有帧（流式增量 / 主题切换 / 会话回放）
  用 `runJavaScript` 只替换 `#stream` 的 innerHTML —— 不再整页重载。动机：
  1. `setHtml` 每次都是页面重载：旧页销毁 → 新渲染表面，切换瞬间露出未初始化帧
     （黑屏 / 爆闪，亮暗两态都有）；
  2. `setHtml` 走 data: URL，**内容超 2MB 直接 loadFinished(success=false)** ——
     长对话渲染失败的隐藏坑；JS 局部更新无此限制；
  3. 整页重载会把滚动位置重置到顶部：流式输出期间用户上翻会被硬拽回去；
     JS 更新保持滚动，且仅在「原本就在底部」时自动跟底。
- **惰性创建**（rev19）：WebEngine 首视图要拉起 GPU/渲染子进程（启动慢的大头），
  推迟到首条消息才创建视图；空状态先上屏。
- 隐藏期重放（rev12）：不可见时置脏，`showEvent` 重放。
- 安全（rev15）：消息内链接一律系统浏览器打开；CSP `default-src 'none'` 拦内容侧脚本
  （`runJavaScript` 是嵌入方 API，不受页面 CSP 约束，更新通道不会被自己拦掉）。
"""

from __future__ import annotations

import importlib.util
import json
import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from gui.theme import DEFAULT_FONT_SIZE, DEFAULT_THEME
from gui.widgets.render.md import assemble, markdown_inner, stub_doc


def webengine_available() -> bool:
    if os.environ.get("YMT_NO_WEBENGINE"):
        return False
    return importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None


def _open_external(url: QUrl) -> None:
    QDesktopServices.openUrl(url)


def _make_external_page(parent) -> object:
    """构造「链接点击 → 系统浏览器」的页面（rev22：提到模块级，避免每次建视图都定义类）。

    嵌入方 API：QWebEnginePage 只在此处 import（WebEngine 缺失时整个分支不会走到）。
    """
    from PySide6.QtWebEngineCore import QWebEnginePage

    class _ExternalPage(QWebEnginePage):
        def acceptNavigationRequest(self, url, ntype, is_main_frame):  # noqa: N802
            if is_main_frame and ntype == QWebEnginePage.NavigationTypeLinkClicked:
                _open_external(url)
                return False
            return super().acceptNavigationRequest(url, ntype, is_main_frame)

    return _ExternalPage(parent)


#: 距底多少像素内视为「在底部」（更新后自动跟底；上翻阅读则不打扰）
NEAR_BOTTOM_PX = 64


class RendererView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.using_webengine = webengine_available()
        self._view: QWidget | None = None  # 惰性创建（见模块 docstring）
        self._inner = ""
        self._dirty = False
        self._bg: str | None = None
        self._loaded = False  # WebEngine：初始壳 loadFinished 已到
        self._loading = False  # 初始壳加载中（期间的新帧排队）
        self._pending: str | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    # -- 视图创建 ----------------------------------------------------------
    def _ensure_view(self) -> None:
        if self._view is not None:
            return
        if self.using_webengine:
            try:
                from PySide6.QtWebEngineCore import QWebEngineSettings
                from PySide6.QtWebEngineWidgets import QWebEngineView

                view = QWebEngineView(self)
                view.setPage(_make_external_page(view))
                settings = view.settings()
                # JS 必须开：局部更新通道走 runJavaScript；内容侧脚本由 CSP 拦（default-src 'none'）
                settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
                settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, False)
                settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
                view.loadFinished.connect(self._on_load_finished)
                self._view = view
            except Exception:  # noqa: BLE001 - 运行期初始化失败则降级
                self.using_webengine = False
                self._view = self._make_browser()
        else:
            self._view = self._make_browser()
        self._layout.addWidget(self._view)

    @staticmethod
    def _make_browser() -> QTextBrowser:
        browser = QTextBrowser()
        browser.setOpenLinks(False)  # 不得在应用内导航
        browser.anchorClicked.connect(_open_external)
        return browser

    # -- 内容 --------------------------------------------------------------
    def set_stream(self, inner: str, bg: str | None = None, jump_bottom: bool = False) -> None:
        """整帧更新流内容（innerHTML 片段）。

        WebEngine 路径：首帧加载空壳 + 排队；壳就绪后（loadFinished）本帧与后续帧
        全部走 JS 局部更新。QTextBrowser 路径：直接整文档 setHtml（无重载/2MB 问题）。
        bg：主题背景色（壳底色，防加载瞬间露白）。
        jump_bottom：无条件回到底部（rev21，会话切换/清空用）——
        缺省只在「原本就在底部」时跟底，不拽走上翻阅读的用户。
        """
        self._inner = inner
        self._dirty = not self.isVisible()
        self._ensure_view()
        if bg and bg != self._bg:
            self._bg = bg
            self._apply_background()
        if self.using_webengine:
            if self._loaded:
                self._run_update(inner, jump_bottom)
            elif self._loading:
                self._pending = (inner, jump_bottom)  # 壳加载中：最新帧排队
            else:
                self._loading = True
                self._view.setHtml(stub_doc())  # 空壳恒小于 2MB 上限
                self._pending = (inner, jump_bottom)
        else:
            self._view.setHtml(assemble(inner))
            self._scroll_bottom()
            self._view.update()

    def set_markdown(
        self,
        text: str,
        theme: str | None = DEFAULT_THEME,
        font_size: str | None = DEFAULT_FONT_SIZE,
    ) -> None:
        """渲染 Markdown；字号档位必须一并透传，否则调用方无法随外观设置缩放。"""
        self.set_stream(markdown_inner(text, theme, font_size))

    @staticmethod
    def _update_script(inner: str, jump_bottom: bool = False) -> str:
        """局部更新脚本：替换 #stream 内容；跳底 = 无条件（rev21）或在底部才跟。"""
        payload = json.dumps(inner, ensure_ascii=False)  # 合法 JS 字符串字面量（任意内容安全转义）
        scroll = (
            "window.scrollTo(0,document.body.scrollHeight);"
            if jump_bottom
            else "if(nb){window.scrollTo(0,document.body.scrollHeight);}"
        )
        return (
            "var nb=(window.innerHeight+window.scrollY)"
            f">=document.body.scrollHeight-{NEAR_BOTTOM_PX};"
            f"document.getElementById('stream').innerHTML={payload};"
            f"{scroll}"
        )

    def _run_update(self, inner: str, jump_bottom: bool = False) -> None:
        self._view.page().runJavaScript(self._update_script(inner, jump_bottom))

    def scroll_to(self, index: int) -> None:
        """滚动到第 index 条消息（rev24：右侧问题列表跳转；`id="m{index}"` 锚点）。"""
        if self._view is None:
            return
        if self.using_webengine:
            if self._loaded:
                self._view.page().runJavaScript(
                    f"var e=document.getElementById('m{index}');if(e){{e.scrollIntoView({{block:'start'}});}}"
                )
        else:
            self._view.scrollToAnchor(f"m{index}")

    def _scroll_bottom(self) -> None:
        bar = self._view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())

    def _on_load_finished(self, ok: bool) -> None:
        self._loading = False
        self._loaded = True
        if self._pending is not None:
            (inner, jump), self._pending = self._pending, None
            self._run_update(inner, jump)

    # -- 外观 --------------------------------------------------------------
    def _apply_background(self) -> None:
        if not self._bg:
            return
        if self.using_webengine and self._view is not None:
            try:
                from PySide6.QtCore import QColor

                self._view.page().setBackgroundColor(QColor(self._bg))
            except Exception:  # noqa: BLE001 - 底色失败不影响内容
                pass
        elif self._view is not None:
            self._view.setStyleSheet(f"QTextBrowser {{ background: {self._bg}; }}")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if self._dirty and self._inner:
            self._dirty = False
            self._replay()

    def _replay(self) -> None:
        """隐藏期置脏后的重放（rev12）：WebEngine 对隐藏视图的加载可能被丢弃。"""
        if self._view is None:
            self._ensure_view()
        if self.using_webengine:
            if self._loaded:
                self._run_update(self._inner)
            else:
                self._loading = True
                self._view.setHtml(stub_doc())
                self._pending = (self._inner, False)
        else:
            self._view.setHtml(assemble(self._inner))
            self._view.update()
