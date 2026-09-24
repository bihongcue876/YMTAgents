"""命令面板（输入区上方的命令候选列表）。

只呈现 `gui.chat.commands` 的命令表；不持有任何 core 引用、不构造任何请求。
键盘由输入区转发（上/下选择、Enter 执行、Esc 隐藏），保证焦点始终留在输入框。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QListWidget, QListWidgetItem, QVBoxLayout

from gui.chat.commands import Command, match


class CommandPalette(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("commandPalette")
        self._commands: list[Command] = []
        self.setFrameShape(QFrame.NoFrame)
        self._hint = QLabel("命令：↑↓ 选择 · Enter 执行 · Esc 关闭")
        self._hint.setObjectName("mutedNote")
        self._list = QListWidget()
        self._list.setObjectName("commandList")
        self._list.setFocusPolicy(Qt.NoFocus)  # 焦点留在输入框
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        layout.addWidget(self._hint)
        layout.addWidget(self._list)
        self.setVisible(False)
        self.setMaximumHeight(220)

    # -- 数据 --------------------------------------------------------------
    def refresh(self, text: str) -> None:
        """按当前输入过滤候选；空结果时隐藏。"""
        commands = match(text)
        self._commands = commands
        self._list.clear()
        for command in commands:
            usage = f" {command.usage}" if command.usage else ""
            item = QListWidgetItem(f"/{command.name}{usage}　—　{command.summary}")
            self._list.addItem(item)
        if commands:
            self._list.setCurrentRow(0)
        self.setVisible(bool(commands))

    def current_command(self) -> Command | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._commands):
            return self._commands[row]
        return None

    def has_selection(self) -> bool:
        return self._list.currentRow() >= 0 and bool(self._commands)

    def move_selection(self, delta: int) -> None:
        if not self._commands:
            return
        row = (self._list.currentRow() + delta) % len(self._commands)
        self._list.setCurrentRow(row)

    def hide_palette(self) -> None:
        self.setVisible(False)