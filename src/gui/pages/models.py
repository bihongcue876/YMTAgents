"""模型配置页：BYOK 多供应商 + 自定义端点 + 测试连接 + 槽位绑定（rev2 §1.4）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.envelope import ModelSpec, ProviderSpec
from shared.ids import PRV, new_id

PRESETS = {
    "OpenAI": "https://api.openai.com/v1",
    "DeepSeek": "https://api.deepseek.com/v1",
    "Moonshot": "https://api.moonshot.cn/v1",
    "SiliconFlow": "https://api.siliconflow.cn/v1",
}


class ProviderDialog(QDialog):
    def __init__(self, provider: ProviderSpec | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("供应商")
        self._existing = provider
        self._provider_id = provider.id if provider else new_id(PRV)

        self._name = QLineEdit(provider.name if provider else "")
        self._base = QComboBox()
        self._base.setEditable(True)
        for label, url in PRESETS.items():
            self._base.addItem(label, url)
        if provider:
            self._base.setCurrentText(provider.base_url)
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.Password)
        self._key.setPlaceholderText("留空表示保持不变" if provider else "API Key")

        self._models = QTableWidget(0, 2)
        self._models.setHorizontalHeaderLabels(["模型 ID", "上下文窗口"])
        if provider:
            for model in provider.models:
                self._add_row(model.id, model.ctx_window)
        add_row = QPushButton("添加模型")
        add_row.clicked.connect(lambda: self._add_row("", 0))
        del_row = QPushButton("删除选中模型")
        del_row.clicked.connect(self._remove_selected)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("名称", self._name)
        form.addRow("base_url", self._base)
        form.addRow("API Key", self._key)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._models)
        row = QHBoxLayout()
        row.addWidget(add_row)
        row.addWidget(del_row)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _add_row(self, model_id: str, ctx_window: int) -> None:
        row = self._models.rowCount()
        self._models.insertRow(row)
        self._models.setItem(row, 0, QTableWidgetItem(model_id))
        self._models.setItem(row, 1, QTableWidgetItem(str(ctx_window)))

    def _remove_selected(self) -> None:
        for index in sorted({i.row() for i in self._models.selectedIndexes()}, reverse=True):
            self._models.removeRow(index)

    def result_spec(self) -> ProviderSpec:
        models: list[ModelSpec] = []
        for row in range(self._models.rowCount()):
            id_item = self._models.item(row, 0)
            ctx_item = self._models.item(row, 1)
            model_id = (id_item.text().strip() if id_item else "")
            if not model_id:
                continue
            try:
                ctx = int(ctx_item.text()) if ctx_item else 0
            except ValueError:
                ctx = 0
            models.append(ModelSpec(id=model_id, ctx_window=ctx))
        return ProviderSpec(
            id=self._provider_id,
            name=self._name.text().strip() or "未命名",
            base_url=self._base.currentData() or self._base.currentText().strip(),
            models=models,
        )

    def api_key(self) -> str | None:
        text = self._key.text().strip()
        return text or None


class ModelsPage(QWidget):
    upsert_requested = Signal(object, object)  # ProviderSpec, api_key | None
    delete_requested = Signal(str)
    test_requested = Signal(str, str)  # provider_id, model_id
    slot_requested = Signal(str, str)  # slot, model_id ("" = 未绑定)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._providers: list[ProviderSpec] = []
        self._test_labels: dict[tuple[str, str], QLabel] = {}

        title = QLabel("模型配置")
        title.setStyleSheet("font-size:16px;font-weight:600;")
        add = QPushButton("添加供应商")
        add.clicked.connect(self._on_add)

        self._list = QVBoxLayout()
        self._list.setAlignment(Qt.AlignTop)

        self._main_slot = QComboBox()
        self._main_slot.currentIndexChanged.connect(self._on_slot_changed)
        self._other_slots = []
        slot_form = QFormLayout()
        slot_form.addRow("main", self._main_slot)
        for name in ("thinking", "fast", "embedding"):
            combo = QComboBox()
            combo.addItem("随轮次启用", "")
            combo.setEnabled(False)
            self._other_slots.append(combo)
            slot_form.addRow(name, combo)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(add)
        layout.addLayout(self._list)
        layout.addSpacing(12)
        layout.addWidget(QLabel("槽位绑定"))
        layout.addLayout(slot_form)
        self._loading = False

    # -- 更新 --------------------------------------------------------------
    def update_providers(self, providers, slots) -> None:
        self._providers = list(providers)
        self._rebuild()

        self._loading = True
        self._main_slot.clear()
        self._main_slot.addItem("未绑定", "")
        for provider in self._providers:
            for model in provider.models:
                self._main_slot.addItem(f"{provider.name} / {model.id}", model.id)
        current = slots.get("main") or ""
        index = self._main_slot.findData(current)
        if index >= 0:
            self._main_slot.setCurrentIndex(index)
        self._loading = False

    def _rebuild(self) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._test_labels.clear()

        if not self._providers:
            self._list.addWidget(QLabel("尚无供应商。点击「添加供应商」开始。"))
            return

        for provider in self._providers:
            self._list.addWidget(self._make_card(provider))

    def _make_card(self, provider: ProviderSpec) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(card)

        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{provider.name}</b>"))
        status = "已存储" if provider.key_status == "stored" else "未设置"
        badge = QLabel(status)
        badge.setStyleSheet(
            "color:#16A34A;" if provider.key_status == "stored" else "color:#DC2626;"
        )
        header.addWidget(badge)
        header.addStretch(1)
        edit = QPushButton("编辑")
        edit.clicked.connect(lambda _=False, p=provider: self._on_edit(p))
        delete = QPushButton("删除")
        delete.clicked.connect(lambda _=False, p=provider: self._on_delete(p))
        header.addWidget(edit)
        header.addWidget(delete)
        layout.addLayout(header)

        layout.addWidget(QLabel(provider.base_url))
        for model in provider.models:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"· {model.id}"))
            test = QPushButton("测试")
            test.clicked.connect(
                lambda _=False, pid=provider.id, mid=model.id: self.test_requested.emit(pid, mid)
            )
            label = QLabel("")
            self._test_labels[(provider.id, model.id)] = label
            row.addWidget(test)
            row.addWidget(label)
            row.addStretch(1)
            layout.addLayout(row)
        return card

    def on_test_result(self, event) -> None:
        label = self._test_labels.get((event.provider_id, event.model_id))
        if label is None:
            return
        if event.ok:
            label.setText(f"成功 · {event.latency_ms}ms")
            label.setStyleSheet("color:#16A34A;")
        else:
            label.setText(f"失败 · {event.error}")
            label.setStyleSheet("color:#DC2626;")

    # -- 交互 --------------------------------------------------------------
    def _on_add(self) -> None:
        dialog = ProviderDialog(None, self)
        if dialog.exec() == QDialog.Accepted:
            self.upsert_requested.emit(dialog.result_spec(), dialog.api_key())

    def _on_edit(self, provider: ProviderSpec) -> None:
        dialog = ProviderDialog(provider, self)
        if dialog.exec() == QDialog.Accepted:
            self.upsert_requested.emit(dialog.result_spec(), dialog.api_key())

    def _on_delete(self, provider: ProviderSpec) -> None:
        if QMessageBox.question(self, "删除供应商", f"确定删除「{provider.name}」？") == QMessageBox.Yes:
            self.delete_requested.emit(provider.id)

    def _on_slot_changed(self, _index: int) -> None:
        if self._loading:
            return
        self.slot_requested.emit("main", self._main_slot.currentData() or "")
