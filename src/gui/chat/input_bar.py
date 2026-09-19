"""输入区：2–10 行自适应；Ctrl+Enter 发送 / Enter 换行；生成中可打字不可重发（rev24–25）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTextEdit, QWidget

# rev25（用户裁决）：默认 2 行；随输入增高至多 10 行；发送清空后自动回到 2 行。
_MIN_LINES = 2
_MAX_LINES = 10
_LINE_PX = 22


class _InputEdit(QTextEdit):
    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # rev24（用户裁决）：Enter 换行、Ctrl+Enter 发送；发送按钮另设。
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (
            event.modifiers() & Qt.ControlModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class InputBar(QWidget):
    send_message = Signal(str)
    cancel = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = _InputEdit()
        self._edit.setPlaceholderText("输入消息，Ctrl+Enter 发送，Enter 换行")
        self._edit.submitted.connect(self._on_send)
        self._edit.textChanged.connect(self._adjust_height)
        self._generating = False

        self._button = QPushButton("发送")
        self._button.clicked.connect(self._on_button)

        layout = QHBoxLayout(self)
        layout.addWidget(self._edit, 1)
        layout.addWidget(self._button)
        self._adjust_height()

    def _adjust_height(self) -> None:
        lines = min(_MAX_LINES, max(_MIN_LINES, self._edit.document().blockCount()))
        self._edit.setFixedHeight(lines * _LINE_PX + 10)

    def _on_send(self) -> None:
        text = self._edit.toPlainText().strip()
        if not text or self._generating:
            return
        self.send_message.emit(text)
        self._edit.clear()

    def _on_button(self) -> None:
        if self._generating:
            self.cancel.emit()
        else:
            self._on_send()

    def set_generating(self, generating: bool) -> None:
        self._generating = generating
        self._button.setText("■ 停止" if generating else "发送")

    def clear(self) -> None:
        self._edit.clear()

    def focus(self) -> None:
        """把焦点给输入框（空状态 CTA「开始对话」与新建会话后使用）。"""
        self._edit.setFocus()
