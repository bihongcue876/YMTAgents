"""模型配置界面适配测试（自定义模型/API 选择、全局文本快捷键）。"""

from __future__ import annotations

import pytest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

from gui.pages.models import ModelsPage, ProviderDialog
from gui.widgets import text_shortcuts


def test_main_slot_supports_custom_model(qapp):
    page = ModelsPage()
    assert page._main_slot.isEditable() is True
    # 输入自定义模型并经编辑完成触发
    emitted: list = []
    page.slot_requested.connect(lambda slot, model: emitted.append((slot, model)))
    page._main_slot.setEditText("my-custom-model")
    page._main_slot.lineEdit().editingFinished.emit()
    assert ("main", "my-custom-model") in emitted


def test_provider_dialog_kind_row(qapp):
    dialog = ProviderDialog(None)
    assert dialog._kind.count() == 2
    assert dialog._kind.itemText(0) == "预设供应商"
    assert dialog._kind.itemText(1) == "自定义模型 API"
    # 切换到自定义时清空 base_url
    dialog._loading = False
    dialog._base.setEditText("https://api.example.com/v1")
    dialog._kind.setCurrentIndex(1)
    assert dialog._base.currentText() == "" or dialog._base.currentText() != "https://api.example.com/v1"


def test_provider_dialog_existing_custom_detected(qapp):
    from shared.envelope import ProviderSpec

    spec = ProviderSpec(id="prv", name="X", base_url="https://custom.local/v1")
    dialog = ProviderDialog(spec)
    assert dialog._kind.currentIndex() == 1  # 非预置地址 => 自定义模型 API


@pytest.mark.parametrize("key,action,payload", [("hello", "copy", "hello")])
def test_global_shortcut_table_copy(qapp, key, action, payload):
    text_shortcuts.install_text_shortcuts()
    table = QTableWidget(1, 1)
    table.setItem(0, 0, QTableWidgetItem("hello"))
    table.show()
    table.setCurrentCell(0, 0)
    table.setFocus()
    QApplication.processEvents()
    QTest.keyClick(table, Qt.Key_C, Qt.ControlModifier)
    assert QApplication.clipboard().text() == "hello"
    table.close()