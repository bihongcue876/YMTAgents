"""角色配置页（阶段 2 · spec rev23）：Persona = 全局配置，形同模型。

- 角色卡片列表（YMT 预置 ★ 标记、当前默认「默认」徽标、当前会话「使用中」徽标）；
- 编辑对话框：名称 + 提示词（纯文本编辑，独立 prompt.md 落盘便于手编与 diff）；
- 操作：设为默认（新会话用它）/ 编辑 / 删除（YMT 不可删）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class PersonaDialog(QDialog):
    """新建 / 编辑角色：名称 + 提示词。"""

    def __init__(
        self,
        persona_id: str | None = None,
        name: str = "",
        prompt: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.persona_id = persona_id
        self.setWindowTitle("编辑角色" if persona_id else "新建角色")

        self._name = QLineEdit(name)
        self._name.setPlaceholderText("角色名称（如：代码评审助手）")
        self._prompt = QPlainTextEdit(prompt)
        self._prompt.setPlaceholderText("系统提示词本体（Markdown）。角色是谁、按什么风格与规则行事……")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("名称", self._name)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("提示词（系统提示词本体）"))
        layout.addWidget(self._prompt, 1)
        layout.addWidget(buttons)
        self.resize(680, 560)
        self.setMinimumSize(560, 460)

    def _on_accept(self) -> None:
        if not self._name.text().strip():
            self._name.setFocus()
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        """(名称, 提示词)。"""
        return self._name.text().strip(), self._prompt.toPlainText()


class PersonasPage(QWidget):
    save_requested = Signal(object, str, str)  # persona_id | None, name, prompt
    delete_requested = Signal(str)
    set_default_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = QLabel("角色配置")
        self._title.setObjectName("pageTitle")
        add = QPushButton("新建角色")
        add.clicked.connect(self._on_add)
        self._list = QVBoxLayout()
        self._list.setAlignment(Qt.AlignTop)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(add)
        layout.addLayout(self._list)
        layout.addStretch(1)
        self._personas: list = []

    def update_personas(self, event) -> None:
        """重建卡片列表（persona.list 事件）。"""
        self._personas = list(event.personas)
        while self._list.count():
            item = self._list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for p in self._personas:
            self._list.addWidget(self._make_card(p))

    # -- 卡片 --------------------------------------------------------------
    def _make_card(self, p) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(card)

        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{p.name}</b>"))
        if p.is_default:
            header.addWidget(self._badge("默认", True))
        if p.in_session:
            header.addWidget(self._badge("使用中", True))
        if p.builtin:
            header.addWidget(self._badge("预置", None))
        header.addStretch(1)
        layout.addLayout(header)

        preview = p.prompt.strip().splitlines()[0] if p.prompt.strip() else "（空提示词）"
        body = QLabel(preview)
        body.setWordWrap(True)
        layout.addWidget(body)

        actions = QHBoxLayout()
        set_default = QPushButton("设为默认")
        set_default.setEnabled(not p.is_default)
        set_default.clicked.connect(lambda _=False, pid=p.id: self.set_default_requested.emit(pid))
        actions.addWidget(set_default)
        edit = QPushButton("编辑")
        edit.clicked.connect(lambda _=False, pid=p.id: self._on_edit(pid))
        actions.addWidget(edit)
        delete = QPushButton("删除")
        delete.setEnabled(not p.builtin)
        delete.clicked.connect(lambda _=False, pid=p.id: self.delete_requested.emit(pid))
        actions.addWidget(delete)
        actions.addStretch(1)
        layout.addLayout(actions)
        return card

    @staticmethod
    def _badge(text: str, ok: bool | None) -> QLabel:
        badge = QLabel(text)
        badge.setObjectName("keyBadge")
        if ok is not None:
            badge.setProperty("keyStored", ok)
        return badge

    # -- 动作 --------------------------------------------------------------
    def _on_add(self) -> None:
        dialog = PersonaDialog(parent=self)
        if dialog.exec() == QDialog.Accepted:
            name, prompt = dialog.values()
            if name:
                self.save_requested.emit(None, name, prompt)

    def _on_edit(self, persona_id: str) -> None:
        info = next((p for p in self._personas if p.id == persona_id), None)
        if info is None:
            return
        dialog = PersonaDialog(persona_id=persona_id, name=info.name, prompt=info.prompt, parent=self)
        if dialog.exec() == QDialog.Accepted:
            name, prompt = dialog.values()
            if name:
                self.save_requested.emit(persona_id, name, prompt)
