"""对话视图：头条 + 消息流 + 输入区（spec §3.2 / rev2 §1.3）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from gui.chat.empty_state import EmptyState
from gui.chat.header import ChatHeader
from gui.chat.input_bar import InputBar
from gui.chat.message_list import MessageList


class ChatView(QWidget):
    send_message = Signal(str)
    cancel_turn = Signal()
    switch_model = Signal(str)
    rename_session = Signal(str)
    new_session = Signal()
    add_model = Signal()  # 空状态 CTA：跳模型配置页（rev9 §6）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.header = ChatHeader()
        self.messages = MessageList()
        self.empty = EmptyState()
        self.input = InputBar()

        # 空状态与消息流互斥显示（rev2 §1.3）
        self._stack = QStackedWidget()
        self._stack.addWidget(self.empty)
        self._stack.addWidget(self.messages)

        layout = QVBoxLayout(self)
        layout.addWidget(self.header)
        layout.addWidget(self._stack, 1)
        layout.addWidget(self.input)

        self.header.rename.connect(self.rename_session.emit)
        self.header.switch_model.connect(self.switch_model.emit)
        self.header.new_session.connect(self.new_session.emit)
        self.input.send_message.connect(self._on_send)
        self.input.cancel.connect(self.cancel_turn.emit)
        self.empty.add_model.connect(self.add_model.emit)
        self.empty.start_chat.connect(self.input.focus)
        self._current_model: str | None = None
        self._refresh_empty()

    def _refresh_empty(self) -> None:
        """空状态 ⇄ 消息流的**唯一**切换点。"""
        self._stack.setCurrentWidget(self.empty if self.messages.is_empty() else self.messages)

    def _on_send(self, text: str) -> None:
        self.messages.add_user(text)
        self._refresh_empty()
        self.send_message.emit(text)

    def set_models(self, providers, current: str | None) -> None:
        self._current_model = current
        self.header.set_models(providers, current)
        # 空状态文案随「有无供应商」切换：无 → 添加模型；有 → 开始对话
        self.empty.set_has_provider(bool(providers))

    def set_title(self, title: str) -> None:
        self.header.set_title(title)

    def set_theme(self, name: str | None, font_size: str | None = None) -> None:
        """外观切换：消息流需整帧重渲染，其余控件由全局 QSS 换肤/重排。"""
        self.messages.set_theme(name, font_size)

    def clear(self) -> None:
        """清空消息流（新建会话时），并回到空状态。"""
        self.messages.clear()
        self._refresh_empty()

    def load_session(self, title: str, events: list[dict]) -> None:
        self.header.set_title(title)
        self.messages.load_events(events)
        self.input.set_generating(False)
        self._refresh_empty()

    # -- 事件处理 ----------------------------------------------------------
    def on_delta(self, event) -> None:
        self.messages.append_delta(event.content)
        self._refresh_empty()

    def on_final(self, event) -> None:
        usage = event.usage
        model = self._current_model or ""
        usage_text = f"{usage.total_tokens} tokens · {model}".strip(" ·")
        self.messages.finalize(event.content, usage_text, event.interrupted)
        self.input.set_generating(False)
        self._refresh_empty()

    def on_status(self, event) -> None:
        if event.state in ("assembling", "calling"):
            self.input.set_generating(True)
        else:
            self.input.set_generating(False)

    def on_error(self, event) -> None:
        self.messages.add_error(event.message, event.detail)
        self.input.set_generating(False)
        self._refresh_empty()
