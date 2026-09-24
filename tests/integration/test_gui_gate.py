"""GUI 关卡卡片：对话框内确认 + 对照模式（v0.0.11 切片 B）。"""

from __future__ import annotations

from shared.envelope import GateRequest

from gui.chat.gate_card import GateCard, GateStack, diff_text
from gui.chat.view import ChatView


def _event(preview=None, args=None, name="file.edit"):
    return GateRequest(
        call_id="c1",
        name=name,
        args=args if args is not None else {"path": "a.txt"},
        permission="confirm",
        preview=preview or {},
    )


def test_diff_text_marks_removed_and_added_lines():
    text = diff_text({"kind": "edit", "old": "a\nb\n", "new": "a\nB\n"})
    assert "- a" in text and "- b" in text and "+ a" in text and "+ B" in text


def test_diff_text_write_new_file():
    text = diff_text({"kind": "write", "old": "", "new": "hello\n"})
    assert "（新建文件，无旧文）" in text and "+ hello" in text


def test_card_shows_diff_and_reports_decision(qapp):
    seen: list[tuple[str, bool]] = []
    card = GateCard(_event({"kind": "edit", "old": "x", "new": "y"}))
    card.decided.connect(lambda cid, allow: seen.append((cid, allow)))
    body = card.detail.toPlainText()
    assert "- x" in body and "+ y" in body
    card.allow.click()
    assert seen == [("c1", True)]
    assert card.resolved() is True
    assert card.record.text() == "等待执行…"
    assert card.detail.isHidden() is True       # 裁决后塌缩
    card.resolve("已修改 a.txt（+1 / -1）", ok=True)
    assert card.record.text() == "已修改 a.txt（+1 / -1）"
    card.toggle.click()                          # 可再展开对照
    assert card.detail.isHidden() is False
    card.toggle.click()
    assert card.detail.isHidden() is True


def test_card_deny_collapses_immediately(qapp):
    card = GateCard(_event())
    card.deny.click()
    assert "已拒绝（用户）" in card.record.text()


def test_card_ignores_second_decision(qapp):
    seen: list[tuple[str, bool]] = []
    card = GateCard(_event())
    card.decided.connect(lambda cid, allow: seen.append((cid, allow)))
    card.deny.click()
    card.allow.click()  # 已裁决：不再二次回发
    assert seen == [("c1", False)]


def test_card_falls_back_to_args_without_preview(qapp):
    card = GateCard(_event(args={"path": "a.txt", "old_string": "x"}))
    assert "old_string" in card.detail.toPlainText()


def test_stack_tracks_resolves_and_clears(qapp):
    stack = GateStack()
    got: list[tuple[str, bool]] = []
    stack.decided.connect(lambda cid, allow: got.append((cid, allow)))
    stack.show_gate(_event())
    assert stack.count() == 1 and stack.isHidden() is False
    assert stack.resolve("c1", "已完成") is True
    assert stack.resolve("nope", "x") is False
    stack.clear()
    assert stack.count() == 0 and stack.isHidden() is True


def test_chat_view_hosts_gate_stack(qapp):
    view = ChatView()
    got: list[tuple[str, bool]] = []
    view.gate_decided.connect(lambda cid, allow: got.append((cid, allow)))
    card = view.gates.show_gate(_event())
    assert view.gates.count() == 1
    card.allow.click()
    assert got == [("c1", True)]
    view.clear()                                 # 新建会话：卡片一并清空
    assert view.gates.count() == 0
