"""消息流（整段渲染 + 合帧刷新，docs 05 §3）。

单个渲染视图承载整条消息流；流式增量经 50ms 合帧后整帧替换。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget

from gui.theme import DEFAULT_FONT_SIZE, DEFAULT_THEME
from gui.widgets.render.md import messages_to_html
from gui.widgets.render.view import RendererView


class MessageList(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._messages: list[dict] = []
        self._theme = DEFAULT_THEME
        self._font_size = DEFAULT_FONT_SIZE
        self._renderer = RendererView(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._renderer)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._render)

    # -- 外观 --------------------------------------------------------------
    def set_theme(self, name: str | None, font_size: str | None = None) -> None:
        """切换外观并整帧重渲染（消息内容不变，仅换色与字号）。"""
        name = name or DEFAULT_THEME
        font_size = font_size or DEFAULT_FONT_SIZE
        if name != self._theme or font_size != self._font_size:
            self._theme = name
            self._font_size = font_size
            self._render()

    # -- 渲染 --------------------------------------------------------------
    def _render(self) -> None:
        self._renderer.set_html(messages_to_html(self._messages, self._theme, self._font_size))

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _last_is_assistant(self) -> bool:
        return bool(self._messages) and self._messages[-1].get("role") == "assistant"

    # -- 操作 --------------------------------------------------------------
    def clear(self) -> None:
        self._messages.clear()
        self._render()

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})
        self._render()

    def begin_assistant(self) -> None:
        self._messages.append({"role": "assistant", "content": "", "usage": None, "interrupted": False})
        self._render()

    def append_delta(self, text: str) -> None:
        if not self._last_is_assistant():
            self.begin_assistant()
        self._messages[-1]["content"] += text
        self._schedule()

    def finalize(self, content: str, usage_text: str | None, interrupted: bool) -> None:
        if not self._last_is_assistant():
            self.begin_assistant()
        self._messages[-1]["content"] = content
        self._messages[-1]["usage"] = usage_text
        self._messages[-1]["interrupted"] = interrupted
        self._render()

    def add_error(self, message: str, detail: str | None = None) -> None:
        self._messages.append({"role": "error", "content": message, "detail": detail})
        self._render()

    def load_events(self, events: list[dict]) -> None:
        self._messages.clear()
        for event in events:
            t = event.get("type")
            payload = event.get("payload", {})
            if t == "msg.user":
                self._messages.append({"role": "user", "content": payload.get("text", "")})
            elif t == "msg.assistant.final":
                self._messages.append(
                    {
                        "role": "assistant",
                        "content": payload.get("content", ""),
                        "usage": None,
                        "interrupted": bool(payload.get("interrupted")),
                    }
                )
            elif t == "error":
                self._messages.append(
                    {"role": "error", "content": payload.get("message", ""), "detail": payload.get("detail")}
                )
        self._render()
