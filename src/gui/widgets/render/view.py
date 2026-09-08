"""渲染视图：QWebEngineView 本地离线渲染，QTextBrowser 降级（docs 05 §3）。"""

from __future__ import annotations

import os

from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from gui.widgets.render.md import markdown_to_html


def webengine_available() -> bool:
    if os.environ.get("YMT_NO_WEBENGINE"):
        return False
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


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

    def set_markdown(self, text: str) -> None:
        self._view.setHtml(markdown_to_html(text))
