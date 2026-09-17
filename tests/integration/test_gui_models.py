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
    assert dialog._kind.count() == 3
    assert dialog._kind.itemText(0) == "预设供应商"
    assert dialog._kind.itemText(1) == "自定义模型 API"
    assert dialog._kind.itemText(2) == "本地模型服务（无需密钥）"
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


# -- 本地模型服务（spec rev10 §1） -------------------------------------------


def test_provider_dialog_local_kind_needs_no_key(qapp):
    """本地服务不需要密钥：密钥框禁用、api_key 为空、spec 带 local 标记。"""
    dialog = ProviderDialog(None)
    dialog._loading = False
    dialog._kind.setCurrentIndex(2)
    assert dialog.is_local() is True
    assert dialog._key.isEnabled() is False
    assert dialog.api_key() is None

    dialog._base.setEditText("http://127.0.0.1:11434/v1")
    spec = dialog.result_spec()
    assert spec.local is True
    assert spec.base_url == "http://127.0.0.1:11434/v1"


def test_provider_dialog_auto_switches_on_localhost(qapp):
    """地址填成本机地址即自动切「本地模型服务」，且不吞掉用户刚填的地址。"""
    dialog = ProviderDialog(None)
    dialog._loading = False
    dialog._base.setEditText("http://127.0.0.1:1234/v1")
    assert dialog.is_local() is True
    assert dialog._base.currentText() == "http://127.0.0.1:1234/v1"

    dialog._base.setEditText("https://api.deepseek.com/v1")
    assert dialog.is_local() is False


def test_provider_dialog_local_detected_when_editing(qapp):
    from shared.envelope import ProviderSpec

    spec = ProviderSpec(
        id="prv", name="Ollama", base_url="http://127.0.0.1:11434/v1", local=True
    )
    dialog = ProviderDialog(spec)
    assert dialog.is_local() is True
    assert dialog._key.isEnabled() is False


def test_local_presets_present_and_are_local():
    from gui.pages.models import LOCAL_PRESETS, PRESETS
    from shared.net import is_local_url

    assert "Ollama（本地）" in PRESETS
    for label, url in LOCAL_PRESETS.items():
        assert is_local_url(url), f"{label} 的地址应识别为本机：{url}"


# -- base_url 解析（spec rev10 §3） ------------------------------------------


def test_provider_dialog_typed_url_wins_over_stale_preset(qapp):
    """回归锚点：手输 URL 不得被下拉里残留的选中预设静默替换。

    此前 `result_spec()` 直接取 `currentData()`：在可编辑下拉里输入自定义地址后，
    保存下来的却是**下拉当前项对应的预设地址**，用户看到与存到的不一致。
    """
    dialog = ProviderDialog(None)
    dialog._loading = False
    dialog._name.setText("X")
    dialog._base.setEditText("https://my-endpoint.example.com/v1")
    assert dialog.result_spec().base_url == "https://my-endpoint.example.com/v1"


def test_provider_dialog_preset_label_resolves_to_url(qapp):
    """选中预设**标签**时仍解析为其 URL（便利路径不得被上一条修复破坏）。"""
    dialog = ProviderDialog(None)
    dialog._loading = False
    dialog._base.setCurrentText("DeepSeek")
    assert dialog.result_spec().base_url == "https://api.deepseek.com/v1"


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


# -- 导入即绑定（spec rev10 §2） ---------------------------------------------


def test_model_picker_bind_main_option(qapp):
    """有绑定提示时默认勾选；无提示（main 已正确绑定）则该项不出现。"""
    from gui.pages.models import ModelPickerDialog

    dialog = ModelPickerDialog(["m1", "m2"], bind_hint="同时把第一个选中的模型设为 main 槽位")
    assert dialog.should_bind_main() is True
    dialog._bind_main.setChecked(False)
    assert dialog.should_bind_main() is False

    plain = ModelPickerDialog(["m1"], bind_hint=None)
    assert plain._bind_main is None
    assert plain.should_bind_main() is False


def test_main_bind_hint_covers_three_cases(qapp):
    """未绑定 → 提示；悬空绑定 → 提示改绑并说明当前值；正常绑定 → 不打扰。"""
    from shared.envelope import ProviderSpec

    page = ModelsPage()
    provider = ProviderSpec(id="prv_1", name="P", base_url="https://api.x.com/v1", models=[])

    page.update_providers([provider], {"main": None})
    assert page._main_bind_hint(["m-a"]) is not None

    page.update_providers([provider], {"main": "已废弃的模型"})
    hint = page._main_bind_hint(["m-a", "m-b"])
    assert hint is not None and "已废弃的模型" in hint

    page.update_providers([provider], {"main": "m-a"})
    assert page._main_bind_hint(["m-a", "m-b"]) is None


def _patch_picker(monkeypatch, selected):
    from PySide6.QtWidgets import QDialog

    from gui.pages import models as models_mod

    monkeypatch.setattr(models_mod.ModelPickerDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(
        models_mod.ModelPickerDialog, "selected_models", lambda self: list(selected)
    )


def test_models_page_binds_main_after_import(qapp, monkeypatch):
    """勾选「同时设为 main」后，除 upsert 外还要发 slot_requested —— 否则导入完还不能对话。"""
    from shared.envelope import ModelSpec, ProviderModels, ProviderSpec

    page = ModelsPage()
    page.update_providers(
        [ProviderSpec(id="prv_1", name="P", base_url="https://api.x.com/v1", models=[])],
        {"main": None},
    )
    upserts: list = []
    slots: list = []
    page.upsert_requested.connect(lambda spec, key: upserts.append((spec, key)))
    page.slot_requested.connect(lambda slot, mid: slots.append((slot, mid)))
    _patch_picker(monkeypatch, [ModelSpec(id="m-a"), ModelSpec(id="m-b")])

    page.on_models_result(
        ProviderModels(provider_id="prv_1", ok=True, models=["m-a", "m-b"], error=None)
    )

    assert upserts and upserts[0][1] is None  # api_key=None：密钥不变
    assert [m.id for m in upserts[0][0].models] == ["m-a", "m-b"]
    assert slots == [("main", "m-a")]


def test_models_page_does_not_rebind_when_main_already_set(qapp, monkeypatch):
    """main 已绑定且**就在候选内**则不打扰：不出现绑定选项，也不额外发 slot.set。"""
    from shared.envelope import ModelSpec, ProviderModels, ProviderSpec

    page = ModelsPage()
    page.update_providers(
        [ProviderSpec(id="prv_1", name="P", base_url="https://api.x.com/v1", models=[])],
        {"main": "m-a"},
    )
    slots: list = []
    page.upsert_requested.connect(lambda *_: None)
    page.slot_requested.connect(lambda slot, mid: slots.append((slot, mid)))
    _patch_picker(monkeypatch, [ModelSpec(id="m-a"), ModelSpec(id="m-b")])

    page.on_models_result(
        ProviderModels(provider_id="prv_1", ok=True, models=["m-a", "m-b"], error=None)
    )
    assert slots == []


def test_models_page_rebinds_dangling_main(qapp, monkeypatch):
    """回归锚点：main 指着端点不再提供的模型时，导入必须顺带改绑。

    这正是「换了模型表、main 还指着旧 ID」的真实情形：此时 main 已是悬空绑定，
    若不提示改绑，用户勾完模型仍然不能对话，还得自己去找槽位下拉。
    """
    from shared.envelope import ModelSpec, ProviderModels, ProviderSpec

    page = ModelsPage()
    page.update_providers(
        [
            ProviderSpec(
                id="prv_1",
                name="P",
                base_url="https://api.deepseek.com/v1",
                models=[ModelSpec(id="已废弃的模型")],
            )
        ],
        {"main": "已废弃的模型"},
    )
    upserts: list = []
    slots: list = []
    page.upsert_requested.connect(lambda spec, key: upserts.append(spec))
    page.slot_requested.connect(lambda slot, mid: slots.append((slot, mid)))
    _patch_picker(
        monkeypatch, [ModelSpec(id="deepseek-v4-pro"), ModelSpec(id="deepseek-flash")]
    )

    page.on_models_result(
        ProviderModels(
            provider_id="prv_1",
            ok=True,
            models=["deepseek-v4-pro", "deepseek-flash"],
            error=None,
        )
    )

    assert [m.id for m in upserts[0].models] == ["deepseek-v4-pro", "deepseek-flash"]
    assert slots == [("main", "deepseek-v4-pro")]