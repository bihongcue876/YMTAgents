"""切片 E：复制按钮交互（剪贴板 / 显示开关 / 设置项）。"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from gui.chat.message_list import MessageList
from gui.pages.settings import SettingsPage


def _filled() -> MessageList:
    view = MessageList()
    view.add_user("提问")
    view.begin_assistant()
    view.append_reasoning("先想")
    view.finalize("答案", "", False, "先想")
    view.add_tool_call(
        {"call_id": "c1", "name": "file.read", "args": {"path": "a.txt"}, "permission": "confirm"}
    )
    view.add_tool_result({"call_id": "c1", "ok": True, "output": "1| hi"})
    return view


def test_message_list_resolves_copy_specs(qapp):
    view = _filled()
    assert view._copy_text("msg-raw:1") == "答案"
    assert view._copy_text("msg-md:1") == "答案"
    assert view._copy_text("think-raw:1") == "先想"
    assert view._copy_text("think-md:1") == "> 先想"
    assert view._copy_text("tool-raw:2") == "1| hi"
    assert view._copy_text("tool-md:2").startswith("### 工具调用 · file.read")
    assert view._copy_text("nope:9") == ""
    view._on_copy("msg-raw:1")
    assert QApplication.clipboard().text() == "答案"


def test_copy_buttons_toggle_reaches_renderer(qapp):
    view = MessageList()
    view.set_copy_buttons(False)
    assert view._renderer._body_class() == "no-copy"
    view.set_copy_buttons(True)
    assert view._renderer._body_class() == ""


def test_settings_page_checkbox_round_trip(qapp, tmp_path):
    page = SettingsPage(data_root=str(tmp_path))
    seen: list[tuple[str, dict]] = []
    page.settings_update.connect(lambda section, data: seen.append((section, data)))
    page.load_settings({"ui": {"copy_buttons": False}})
    assert page._copy_buttons.isChecked() is False
    page._copy_buttons.setChecked(True)
    assert seen and seen[-1] == ("ui", {"copy_buttons": True})
