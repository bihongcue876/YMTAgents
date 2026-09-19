"""对话头条：标题就地编辑 + 模型/角色下拉（rev2 §1.3 / rev23）。

角色与模型同构（spec rev23）：全局配置，**会话各自选择** ——
下拉切换只改当前会话；新会话的默认由全局默认决定。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPushButton, QWidget


class ChatHeader(QWidget):
    rename = Signal(str)
    switch_model = Signal(str)
    switch_persona = Signal(str)
    new_session = Signal()
    toggle_detail = Signal()  # rev24：会话详情右栏开关

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = QLineEdit()
        self._title.setPlaceholderText("未命名会话")
        self._title.editingFinished.connect(self._commit_title)

        self._model = QComboBox()
        self._model.setToolTip("选择本会话使用的模型；新对话的默认由全局默认决定")
        self._model.currentIndexChanged.connect(self._on_model_changed)

        self._persona = QComboBox()
        self._persona.setToolTip("选择本会话使用的角色；不同会话可以各用各的角色")
        self._persona.currentIndexChanged.connect(self._on_persona_changed)

        self._new = QPushButton("新建会话")
        self._new.clicked.connect(self.new_session.emit)

        self._detail = QPushButton("详情")
        self._detail.setCheckable(True)
        self._detail.setToolTip("显示 / 隐藏本会话详情与上下文策略面板")
        self._detail.clicked.connect(self.toggle_detail.emit)

        layout = QHBoxLayout(self)
        layout.addWidget(self._title, 1)
        layout.addWidget(self._persona)
        layout.addWidget(self._model)
        layout.addWidget(self._new)
        layout.addWidget(self._detail)
        self._loading = False

    def set_detail_active(self, active: bool) -> None:
        """与右栏实际可见性同步（右栏也可能被自身关闭按钮收起）。"""
        self._detail.setChecked(active)

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

    def set_personas(self, personas, current: str | None) -> None:
        """填充角色下拉（rev23）。personas: PersonaInfo 列表；current = 会话所用角色 id。"""
        self._loading = True
        self._persona.clear()
        for p in personas:
            label = p.name + ("　★" if p.is_default else "")
            self._persona.addItem(label, p.id)
        if current:
            index = self._persona.findData(current)
            if index >= 0:
                self._persona.setCurrentIndex(index)
        self._loading = False

    def _on_model_changed(self, _index: int) -> None:
        if self._loading:
            return
        model_id = self._model.currentData()
        if model_id:
            self.switch_model.emit(model_id)

    def _on_persona_changed(self, _index: int) -> None:
        if self._loading:
            return
        persona_id = self._persona.currentData()
        if persona_id:
            self.switch_persona.emit(persona_id)
