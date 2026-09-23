"""输入区：2–10 行自适应；Ctrl+Enter 发送 / Enter 换行；生成中可打字不可重发（rev24–25）。"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTextEdit, QWidget

# rev25（用户裁决）：默认 2 行；随输入增高至多 10 行；发送清空后自动回到 2 行。
_MIN_LINES = 2
_MAX_LINES = 10
# 纵向富余：覆盖 QTextDocument 上下边距（4+4）与取整误差，保证内容不被裁。
_PAD_PX = 10


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
        # 折行重排也驱动计高：视口变宽/变窄后同一段文本的视觉行数会变，
        # textChanged 不发（内容没变），只有 documentSizeChanged 能捕获。
        self._edit.document().documentLayout().documentSizeChanged.connect(
            lambda _size: self._adjust_height()
        )
        self._generating = False

        self._button = QPushButton("发送")
        self._button.clicked.connect(self._on_button)

        layout = QHBoxLayout(self)
        layout.addWidget(self._edit, 1)
        layout.addWidget(self._button)
        self._adjust_height()

    def _adjust_height(self) -> None:
        """按**视觉行数**计高（rev60 修复）：

        - 行高取 `fontMetrics().lineSpacing()`（随字号档位），不再写死像素 ——
          特大档下固定 22px 会裁掉第 8 行以下的内容（样式表字号不参与
          sizeHint 的已知陷阱在本处的处置）；
        - 行数按 `document().size().height()`（折行后的真实文档高度）折算，
          不用 `blockCount()` —— 后者只数段落，长单行折成十行视觉行仍是 1 块，
          高度被压在 2 行导致内容被裁。
        """
        line_h = self._edit.fontMetrics().lineSpacing()
        doc_h = self._edit.document().size().height()
        if doc_h <= 0:
            # 离屏/未 show 时文档布局不计算（size 恒 0）：按段落数兜底估计，
            # 布局启用后 documentSizeChanged 会立即用真实高度纠正。
            doc_h = self._edit.document().blockCount() * line_h
        lines = min(_MAX_LINES, max(_MIN_LINES, math.ceil(doc_h / line_h)))
        self._edit.setFixedHeight(lines * line_h + _PAD_PX)

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
