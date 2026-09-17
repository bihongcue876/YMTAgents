"""渲染管线单元测试。"""

from __future__ import annotations

from gui import theme
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


def test_page_css_wraps_long_unbroken_strings():
    """回归锚点：正文与用户气泡必须能折行长 URL/长串（rev13）。

    无 overflow-wrap 时长 URL 溢出容器；代码块保留横向滚动（overflow-x: auto）为业界惯例。
    """
    html = markdown_to_html("正文")
    assert "overflow-wrap: anywhere" in html  # body
    assert "overflow-wrap: anywhere" in html and "pre-wrap" in html  # .user .bubble
    assert "overflow-x: auto" in html  # pre 保留横向滚动


def test_page_carries_csp():
    """回归锚点（rev15）：消息流页面必须带 CSP —— 脚本默认全禁（纵深防御）。"""
    html = markdown_to_html("正文")
    assert "Content-Security-Policy" in html
    assert "default-src 'none'" in html
    assert "style-src 'unsafe-inline'" in html  # pygments / 主题样式仍可用
    assert "img-src" in html  # 图片仍可显示，但 connect/frame/script 全禁


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


# -- 字号档位透传 -------------------------------------------------------------


def test_render_honours_font_size():
    """回归锚点：字号档位必须透传到 HTML —— 否则「调字号」对消息流无效。"""
    html = messages_to_html([{"role": "assistant", "content": "x"}], "light", "xlarge")
    assert f"font-size: {theme.font_px('body', 'xlarge')}px" in html
    assert f"font-size: {theme.font_px('code', 'xlarge')}px" in html
    assert f"font-size: {theme.font_px('caption', 'xlarge')}px" in html


def test_render_template_has_no_hardcoded_font_size():
    """旧版把字号写死在模板里（正文 14 / 代码 13 / 小字 12），现改由 theme 注入。"""
    html = markdown_to_html("x")
    assert f"font-size: {theme.font_px('body', 'normal')}px" in html
    assert "font-size: 12px" not in html  # 旧小字基准
    assert "font-size: 13px" not in html  # 旧代码基准
