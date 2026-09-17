"""渲染视图：QWebEngineView 本地离线渲染，QTextBrowser 降级（docs 05 §3）。

安全（rev15）：消息里的链接**一律用系统浏览器打开**，绝不在应用内导航 ——
否则点一个链接就用聊天视图加载任意外部网站（钓鱼页可直接顶掉对话流）。
WebEngine 侧再禁 JS、禁本地文件互访；模板侧加 CSP（md.py）。
"""

from __future__ import annotations

import importlib.util
import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from gui.theme import DEFAULT_FONT_SIZE, DEFAULT_THEME
from gui.widgets.render.md import markdown_to_html


def webengine_available() -> bool:
    if os.environ.get("YMT_NO_WEBENGINE"):
        return False
    return importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None


def _open_external(url: QUrl) -> None:
    QDesktopServices.openUrl(url)


class RendererView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.using_webengine = webengine_available()
        if self.using_webengine:
            try:
                from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
                from PySide6.QtWebEngineWidgets import QWebEngineView

                view = QWebEngineView(self)

                class _ExternalPage(QWebEnginePage):
                    """链接点击 → 系统浏览器；其余导航（setHtml 加载）照常。"""

                    def acceptNavigationRequest(self, url, ntype, is_main_frame):  # noqa: N802
                        if is_main_frame and ntype == QWebEnginePage.NavigationTypeLinkClicked:
                            _open_external(url)
                            return False
                        return super().acceptNavigationRequest(url, ntype, is_main_frame)

                view.setPage(_ExternalPage(view))
                settings = view.settings()
                # 消息流是服务端渲染的纯静态 HTML（pygments 高亮无脚本），JS 只添攻击面
                settings.setAttribute(QWebEngineSettings.JavascriptEnabled, False)
                settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, False)
                settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
                self._view: QWidget = view
            except Exception:  # noqa: BLE001 - 运行期初始化失败则降级
                self.using_webengine = False
                self._view = QTextBrowser(self)
        else:
            browser = QTextBrowser(self)
            browser.setOpenLinks(False)  # 不得在应用内导航
            browser.anchorClicked.connect(_open_external)
            self._view = browser
        self._html = ""
        self._dirty = False
        self._bg: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

    def set_html(self, html: str, bg: str | None = None) -> None:
        """整帧替换页面内容。

        **隐藏期间的内容可能不生效**：WebEngine 对不可见视图的加载/合成会被推迟或丢弃
        （用户从设置页切主题再回对话页时，消息流就停留在旧外观甚至空白）。
        故隐藏时置脏标记，`showEvent` 时重放一次。

        **bg**：主题背景色（rev16 爆闪修复）。`setHtml` 是一次页面重载，
        重载瞬间 WebEngine 露出的是**页面默认底色（白）**——暗色模式下这就是
        用户看到的「切换主题爆闪」。把页面底色设成主题背景即可消除。
        """
        self._html = html
        self._dirty = not self.isVisible()
        if bg and bg != self._bg:
            self._bg = bg
            self._apply_background()
        self._view.setHtml(html)
        self._view.update()

    def _apply_background(self) -> None:
        if not self._bg:
            return
        if self.using_webengine:
            try:
                from PySide6.QtCore import QColor

                self._view.page().setBackgroundColor(QColor(self._bg))
            except Exception:  # noqa: BLE001 - 底色失败不影响内容
                pass
        else:
            self._view.setStyleSheet(f"QTextBrowser {{ background: {self._bg}; }}")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if self._dirty:
            self._dirty = False
            self._view.setHtml(self._html)
            self._view.update()

    def set_markdown(
        self,
        text: str,
        theme: str | None = DEFAULT_THEME,
        font_size: str | None = DEFAULT_FONT_SIZE,
    ) -> None:
        """渲染 Markdown；字号档位必须一并透传，否则调用方无法随外观设置缩放。"""
        self.set_html(markdown_to_html(text, theme, font_size))
