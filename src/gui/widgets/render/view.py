"""渲染视图：QWebEngineView 本地离线渲染，QTextBrowser 降级（docs 05 §3）。"""

from __future__ import annotations

import importlib.util
import os

from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from gui.theme import DEFAULT_FONT_SIZE, DEFAULT_THEME
from gui.widgets.render.md import markdown_to_html


def webengine_available() -> bool:
    if os.environ.get("YMT_NO_WEBENGINE"):
        return False
    return importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None


class RendererView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.using_webengine = webengine_available()
        if self.using_webengine:
            try:
                from PySide6.QtWebEngineWidgets import QWebEngineView

                self._view: QWidget = QWebEngineView(self)
            except Exception:  # noqa: BLE001 - 运行期初始化失败则降级
                self.using_webengine = False
                self._view = QTextBrowser(self)
        else:
            self._view = QTextBrowser(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

    def set_html(self, html: str) -> None:
        self._view.setHtml(html)

    def set_markdown(
        self,
        text: str,
        theme: str | None = DEFAULT_THEME,
        font_size: str | None = DEFAULT_FONT_SIZE,
    ) -> None:
        """渲染 Markdown；字号档位必须一并透传，否则调用方无法随外观设置缩放。"""
        self._view.setHtml(markdown_to_html(text, theme, font_size))
