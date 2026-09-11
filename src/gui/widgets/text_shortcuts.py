"""全局文本快捷键（复制 / 剪切 / 粘贴 / 全选）。

Qt 对 QLineEdit / QTextEdit / QPlainTextEdit（含可编辑 QComboBox 内嵌输入、
表格编辑态的编辑器）默认已支持 Ctrl+C/V/X/A 与撤销等标准快捷键。

本模块补足「未进入编辑态」的表格/列表控件（QAbstractItemView）：
- Ctrl+C：复制当前单元格文本到剪贴板
- Ctrl+X：剪切（复制并清空，仅当单元格可编辑）
- Ctrl+V：粘贴剪贴板文本到当前单元格（仅当单元格可编辑）
- Ctrl+A：全选

安装后作用于整个应用（全局），对标准文本控件透明放行。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication


def _clipboard():
    return QApplication.clipboard()


class _TextShortcutFilter(QObject):
    def eventFilter(self, watched, event) -> bool:
        if event.type() != QEvent.KeyPress:
            return False
        modifiers = event.modifiers()
        if not (modifiers & Qt.ControlModifier):
            return False
        if modifiers & (Qt.AltModifier | Qt.MetaModifier):
            return False

        focus = QApplication.focusWidget()
        if not isinstance(focus, QAbstractItemView):
            return False  # 标准文本控件交给 Qt 默认处理
        if (focus.state().value & QAbstractItemView.State.EditingState.value):
            return False  # 已在编辑态，内部编辑器自己处理
        if focus.currentIndex().isValid() is False:
            return False

        index = focus.currentIndex()
        model = focus.model()
        if model is None:
            return False
        text = model.data(index, Qt.EditRole)
        text = str(text) if text is not None else ""

        key = event.key()
        if key == Qt.Key_C:
            if text:
                _clipboard().setText(text)
            return True
        if key == Qt.Key_X:
            if text:
                _clipboard().setText(text)
            if model.flags(index) & Qt.ItemIsEditable:
                model.setData(index, "", Qt.EditRole)
            return True
        if key == Qt.Key_V:
            data = _clipboard().text()
            if data and (model.flags(index) & Qt.ItemIsEditable):
                model.setData(index, data, Qt.EditRole)
            return True
        if key == Qt.Key_A:
            focus.selectAll()
            return True
        return False


_filter: _TextShortcutFilter | None = None


def install_text_shortcuts() -> None:
    """在 QApplication 上安装全局文本快捷键过滤器（幂等）。"""
    global _filter
    app = QApplication.instance()
    if app is None:
        return
    if _filter is None:
        _filter = _TextShortcutFilter()
    else:
        app.removeEventFilter(_filter)
    app.installEventFilter(_filter)