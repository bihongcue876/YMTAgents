"""渲染管线单元测试。"""

from __future__ import annotations

from gui.widgets.render.md import ansi_to_html, markdown_to_html, messages_to_html, plain_to_html


def test_markdown_basic():
    html = markdown_to_html("# 标题\n\n- a\n- b\n\n`code`")
    assert "<h1>" in html
    assert "<li>" in html
    assert "<code>" in html


def test_markdown_code_highlight():
    html = markdown_to_html("```python\nprint(1)\n```")
    assert "highlight" in html
    assert "print" in html


def test_plain_escapes_html():
    html = plain_to_html("<b>x</b>")
    assert "&lt;b&gt;" in html


def test_ansi_colors():
    html = ansi_to_html("\x1b[31mred\x1b[0m")
    assert "#DC2626" in html
    assert "red" in html


def test_messages_to_html():
    html = messages_to_html(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "**ok**", "usage": "3 tokens · m", "interrupted": False},
            {"role": "error", "content": "boom", "detail": "d"},
        ]
    )
    assert "bubble" in html
    assert "<strong>ok</strong>" in html
    assert "boom" in html
