"""切片 E：复制按钮的渲染层（v0.0.11）。"""

from __future__ import annotations

from gui.widgets.render import md


def _assistant(idx: int = 3) -> dict:
    return {
        "role": "assistant",
        "content": "正文",
        "reasoning": "推理",
        "usage": None,
        "interrupted": False,
    }


def test_assistant_block_has_copy_links():
    html = md.message_row(_assistant(3), 3)
    assert 'href="ymtcopy:msg-md:3"' in html
    assert 'href="ymtcopy:msg-raw:3"' in html
    assert 'href="ymtcopy:think-md:3"' in html
    assert 'href="ymtcopy:think-raw:3"' in html


def test_assistant_without_text_has_no_message_bar():
    msg = _assistant(0)
    msg["content"] = ""
    html = md.message_row(msg, 0)
    assert 'href="ymtcopy:msg-md:0"' not in html
    assert 'href="ymtcopy:think-raw:0"' in html


def test_tool_block_has_copy_links():
    msg = {
        "role": "tool", "name": "file.read", "args": {"path": "a.txt"}, "ok": True,
        "output": "1| hi", "error": None, "permission": "confirm", "duration_ms": 3,
    }
    html = md.message_row(msg, 1)
    assert 'href="ymtcopy:tool-md:1"' in html and 'href="ymtcopy:tool-raw:1"' in html


def test_tool_copy_text_plain_and_markdown():
    msg = {"name": "file.read", "args": {"path": "a.txt"}, "output": "1| hi", "error": None}
    assert md.tool_copy_text(msg) == "1| hi"
    rich = md.tool_copy_text(msg, markdown=True)
    assert rich.startswith("### 工具调用 · file.read")
    assert chr(96) * 3 in rich


def test_tool_copy_text_error_only():
    msg = {"name": "file.edit", "error": {"code": "edit_no_match", "message": "未找到要替换的文本"}}
    assert md.tool_copy_text(msg) == "edit_no_match 未找到要替换的文本"


def test_assemble_and_stub_carry_body_class():
    assert 'class="no-copy"' in md.assemble("", None, "no-copy")
    assert 'class="no-copy"' in md.stub_doc(None, "no-copy")
    assert 'class=""' in md.assemble("")
