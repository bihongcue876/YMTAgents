"""对话头条：标题就地编辑 + 模型下拉（rev2 §1.3）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPushButton, QWidget


class ChatHeader(QWidget):
    rename = Signal(str)
    switch_model = Signal(str)
    new_session = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = QLineEdit()
        self._title.setPlaceholderText("未命名会话")
        self._title.editingFinished.connect(self._commit_title)

        self._model = QComboBox()
        self._model.setToolTip("选择对话使用的模型；新对话将从这里选的模型开始")
        self._model.currentIndexChanged.connect(self._on_model_changed)

        self._new = QPushButton("新建会话")
        self._new.clicked.connect(self.new_session.emit)

        layout = QHBoxLayout(self)
        layout.addWidget(self._title, 1)
        layout.addWidget(self._model)
        layout.addWidget(self._new)
        self._loading = False

    def set_title(self, title: str) -> None:
        if not self._title.hasFocus():
            self._title.setText(title or "")

    def _commit_title(self) -> None:
        self.rename.emit(self._title.text().strip() or "新对话")

    def set_models(self, providers, current: str | None) -> None:
        self._loading = True
        self._model.clear()
        for provider in providers:
            for model in provider.models:
                self._model.addItem(f"{provider.name} / {model.id}", model.id)
        if current:
            index = self._model.findData(current)
            if index >= 0:
                self._model.setCurrentIndex(index)
        self._loading = False

    def _on_model_changed(self, _index: int) -> None:
        if self._loading:
            return
        model_id = self._model.currentData()
        if model_id:
            self.switch_model.emit(model_id)
