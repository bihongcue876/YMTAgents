"""v0.0.11（D-1）：插件页「内置工具」分区（GUI 渲染 + 开关信号）。"""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QLabel

from gui.pages.plugins import PluginsPage

_ITEMS = [
    {"name": "file.read", "title": "读文件", "permission": "confirm", "enabled": True},
    {"name": "file.grep", "title": "按正则搜索", "permission": "confirm", "enabled": False},
]


def test_builtin_card_renders_rows(qapp):
    page = PluginsPage()
    page.update_builtin(_ITEMS)
    labels = [w.text() for w in page.findChildren(QLabel) if "file." in w.text()]
    assert any(text.startswith("file.read") for text in labels)
    assert any(text.startswith("file.grep") for text in labels)
    assert any("内置工具" == w.text() or "内置工具" in w.text() for w in page.findChildren(QLabel))


def test_builtin_toggle_emits_signal(qapp):
    page = PluginsPage()
    got: list[tuple[str, bool]] = []
    page.builtin_toggle_requested.connect(lambda name, on: got.append((name, on)))
    page.update_builtin(_ITEMS)
    boxes = [b for b in page.findChildren(QCheckBox) if b.text() == "启用"]
    assert len(boxes) == 2
    assert {b.isChecked() for b in boxes} == {True, False}
    boxes[0].setChecked(False)
    assert got and got[-1][1] is False


def test_builtin_card_absent_when_empty(qapp):
    page = PluginsPage()
    page.update_builtin([])
    labels = [w.text() for w in page.findChildren(QLabel) if "file." in w.text()]
    assert labels == []
