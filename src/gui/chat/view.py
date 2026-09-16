"""对话视图：头条 + 消息流 + 输入区（spec §3.2 / rev2 §1.3）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from gui.chat.header import ChatHeader
from gui.chat.input_bar import InputBar
from gui.chat.message_list import MessageList


class ChatView(QWidget):
    send_message = Signal(str)
    cancel_turn = Signal()
    switch_model = Signal(str)
    rename_session = Signal(str)
    new_session = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.header = ChatHeader()
        self.messages = MessageList()
        self.input = InputBar()

        layout = QVBoxLayout(self)
        layout.addWidget(self.header)
        layout.addWidget(self.messages, 1)
        layout.addWidget(self.input)

        self.header.rename.connect(self.rename_session.emit)
        self.header.switch_model.connect(self.switch_model.emit)
        self.header.new_session.connect(self.new_session.emit)
        self.input.send_message.connect(self._on_send)
        self.input.cancel.connect(self.cancel_turn.emit)
        self._current_model: str | None = None

    def _on_send(self, text: str) -> None:
        self.messages.add_user(text)
        self.send_message.emit(text)

    def set_models(self, providers, current: str | None) -> None:
        self._current_model = current
        self.header.set_models(providers, current)

    def set_title(self, title: str) -> None:
        self.header.set_title(title)

    def set_theme(self, name: str | None, font_size: str | None = None) -> None:
        """外观切换：消息流需整帧重渲染，其余控件由全局 QSS 换肤/重排。"""
        self.messages.set_theme(name, font_size)

    def load_session(self, title: str, events: list[dict]) -> None:
        self.header.set_title(title)
        self.messages.load_events(events)
        self.input.set_generating(False)

    # -- 事件处理 ----------------------------------------------------------
    def on_delta(self, event) -> None:
        self.messages.append_delta(event.content)

    def on_final(self, event) -> None:
        usage = event.usage
        model = self._current_model or ""
        usage_text = f"{usage.total_tokens} tokens · {model}".strip(" ·")
        self.messages.finalize(event.content, usage_text, event.interrupted)
        self.input.set_generating(False)

    def on_status(self, event) -> None:
        if event.state in ("assembling", "calling"):
            self.input.set_generating(True)
        else:
            self.input.set_generating(False)

    def on_error(self, event) -> None:
        self.messages.add_error(event.message, event.detail)
        self.input.set_generating(False)
