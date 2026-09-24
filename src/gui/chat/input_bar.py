"""输入区：2–10 行自适应；Ctrl+Enter 发送 / Enter 换行；生成中可打字不可重发（rev24–25）。

命令系统（gui.chat.commands）：输入 `/` 打开命令面板（↑↓ 选择、Enter 执行、Esc 关闭）。
命令只是既有按钮动作的键盘入口 —— 执行时发出与点按钮**完全相同**的请求或页面切换。
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget

from gui.chat import commands
from gui.widgets.command_palette import CommandPalette

# rev25（用户裁决）：默认 2 行；随输入增高至多 10 行；发送清空后自动回到 2 行。
_MIN_LINES = 2
_MAX_LINES = 10
# 纵向富余：覆盖 QTextDocument 上下边距（4+4）与取整误差，保证内容不被裁。
_PAD_PX = 10


class _InputEdit(QTextEdit):
    submitted = Signal()
    navigate = Signal(int)  # 命令面板选择移动
    accept = Signal()  # 命令面板回车执行
    dismiss = Signal()  # Esc 关闭命令面板

    def keyPressEvent(self, event: QKeyEvent) -> None:
        command_active = bool(getattr(self, "command_active", False))
        if command_active and event.key() in (Qt.Key_Up, Qt.Key_Down):
            self.navigate.emit(-1 if event.key() == Qt.Key_Up else 1)
            return
        if command_active and event.key() == Qt.Key_Escape:
            self.dismiss.emit()
            return
        # 命令态下 Enter 直接执行面板所选命令；Ctrl+Enter 仍是发送。
        if (
            command_active
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and not (event.modifiers() & Qt.ControlModifier)
        ):
            self.accept.emit()
            return
        # rev24（用户裁决）：Enter 换行、Ctrl+Enter 发送；发送按钮另设。
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (
            event.modifiers() & Qt.ControlModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class InputBar(QWidget):
    send_message = Signal(str, object)  # text, workspace-root-relative attachments
    command_run = Signal(str, str, str)  # name, action, argument
    command_error = Signal(str)
    cancel = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = _InputEdit()
        self._edit.setPlaceholderText("输入消息，Ctrl+Enter 发送，Enter 换行；输入 / 使用命令")
        self._edit.submitted.connect(self._on_send)
        self._edit.navigate.connect(self._on_navigate)
        self._edit.accept.connect(self._run_command)
        self._edit.dismiss.connect(self._dismiss_palette)
        self._edit.textChanged.connect(self._on_text_changed)
        # 折行重排也驱动计高：视口变宽/变窄后同一段文本的视觉行数会变，
        # textChanged 不发（内容没变），只有 documentSizeChanged 能捕获。
        self._edit.document().documentLayout().documentSizeChanged.connect(
            lambda _size: self._adjust_height()
        )
        self._generating = False
        self._attachment_root: Path | None = None
        self._attachments: list[str] = []

        self._palette = CommandPalette(self)

        self._attach = QPushButton("附件")
        self._attach.clicked.connect(self._pick_attachments)

        self._button = QPushButton("发送")
        self._button.clicked.connect(self._on_button)

        row = QHBoxLayout()
        row.addWidget(self._edit, 1)
        row.addWidget(self._attach)
        row.addWidget(self._button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._palette)
        layout.addLayout(row)
        self._adjust_height()

    # -- 命令面板 ----------------------------------------------------------
    def _on_text_changed(self) -> None:
        self._adjust_height()
        text = self._edit.toPlainText()
        active = commands.is_command(text) and "\n" not in text.strip()
        self._edit.command_active = active
        if active:
            self._palette.refresh(text.splitlines()[0] if text.splitlines() else text)
        else:
            self._palette.hide_palette()

    def _on_navigate(self, delta: int) -> None:
        self._palette.move_selection(delta)

    def _dismiss_palette(self) -> None:
        self._palette.hide_palette()
        self._edit.command_active = False

    def _run_command(self) -> None:
        command, argument, error = commands.parse(self._edit.toPlainText())
        if error:
            self.command_error.emit(error)
            return
        if command is None:
            return
        self._edit.clear()
        self._dismiss_palette()
        self.command_run.emit(command.name, command.action, argument)

    # -- 发送 / 附件 -------------------------------------------------------
    def _adjust_height(self) -> None:
        """按**视觉行数**计高（rev60 修复）：
        - 行高取 `fontMetrics().lineSpacing()`（随字号档位），不再写死像素 ——
          特大档下固定 22px 会裁掉第 8 行以下的内容（样式表字号不参与 sizeHint 的已知陷阱在本处的处置）；
        - 行数按 `document().size().height()`（折行后的真实文档高度）折算，不用 `blockCount()`。
        """
        line_h = self._edit.fontMetrics().lineSpacing()
        doc_h = self._edit.document().size().height()
        if doc_h <= 0:
            doc_h = self._edit.document().blockCount() * line_h
        lines = min(_MAX_LINES, max(_MIN_LINES, math.ceil(doc_h / line_h)))
        self._edit.setFixedHeight(lines * line_h + _PAD_PX)

    def _on_send(self) -> None:
        text = self._edit.toPlainText()
        stripped = text.strip()
        # 命令态文本不得被当作普通消息发出（未知命令要显式提示，不静默发送）。
        if commands.is_command(stripped):
            self._run_command()
            return
        if (not stripped and not self._attachments) or self._generating:
            return
        self.send_message.emit(stripped, list(self._attachments))
        self._edit.clear()
        self._attachments.clear()
        self._update_attachment_button()

    def set_attachment_root(self, root: str | Path | None) -> None:
        self._attachment_root = Path(root) if root else None
        self._attachments.clear()
        self._update_attachment_button()

    def _pick_attachments(self) -> None:
        if self._attachment_root is None:
            QMessageBox.information(self, "没有工作区", "附件需要当前工作区目录，请先刷新工作区状态。")
            return
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "选择要挂载的 UTF-8 文本文件", str(self._attachment_root)
        )
        if not paths:
            return
        root = self._attachment_root.resolve(strict=False)
        added: list[str] = []
        for raw in paths:
            path = Path(raw).resolve(strict=False)
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                QMessageBox.warning(self, "附件超出工作区", "只能选择当前工作区 root 内的文件。")
                continue
            if relative not in self._attachments and relative not in added:
                added.append(relative)
        total = len(self._attachments) + len(added)
        if total > 20:
            QMessageBox.warning(self, "附件数量超限", "一次最多挂载 20 个文件。")
            added = added[: max(0, 20 - len(self._attachments))]
        self._attachments.extend(added)
        self._update_attachment_button()

    def _update_attachment_button(self) -> None:
        self._attach.setText(f"附件 ({len(self._attachments)})" if self._attachments else "附件")
        self._attach.setToolTip(
            "\n".join(self._attachments) if self._attachments else "从当前工作区选择 UTF-8 文本文件"
        )

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
        self._attachments.clear()
        self._dismiss_palette()
        self._update_attachment_button()

    def focus(self) -> None:
        """把焦点给输入框（空状态 CTA「开始对话」与新建会话后使用）。"""
        self._edit.setFocus()