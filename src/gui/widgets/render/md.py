"""统一渲染管线：把任意可渲染内容转 HTML（docs 05 §3 统一渲染管线）。

- Markdown → HTML（markdown-it-py + Pygments 代码高亮）
- 纯文本 → 转义 HTML
- ANSI 转义（shell 输出）→ 带色 HTML
输出喂给同一 QWebEngineView（本地离线，无外部 CDN）。

外观：颜色与字号一律来自 `gui.theme`（单一取色 / 取字号来源），
本模块只保留字体族、行高、间距等结构性排版规则。
Pygments 输出的 class 名与样式无关，故高亮回调无需感知主题，仅 CSS 需要。
"""

from __future__ import annotations

import html as _html
import re

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from gui.theme import DEFAULT_FONT_SIZE, DEFAULT_THEME, ansi_colors, markdown_css, pygments_style

_FORMATTER = HtmlFormatter(cssclass="highlight")
_FORMATTERS: dict[str, HtmlFormatter] = {}


def _formatter(theme: str | None) -> HtmlFormatter:
    key = (theme or DEFAULT_THEME).lower()
    if key not in _FORMATTERS:
        _FORMATTERS[key] = HtmlFormatter(cssclass="highlight", style=pygments_style(key))
    return _FORMATTERS[key]


def _pygments_css(theme: str | None) -> str:
    return _formatter(theme).get_style_defs(".highlight")


def _highlight(code: str, lang: str, _attrs: str = "") -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
    except ClassNotFound:
        lexer = get_lexer_by_name("text")
    return highlight(code, lexer, _FORMATTER)


_MD = MarkdownIt("commonmark", {"highlight": _highlight}).enable("table").enable("strikethrough")

_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src http: https: data:;">
<style>
body {{ font-family: system-ui, "Segoe UI", sans-serif; line-height: 1.6;
        margin: 8px 12px; overflow-wrap: anywhere; }}
pre {{ padding: 8px 10px; border-radius: 6px; overflow-x: auto; }}
code {{ font-family: Consolas, "Courier New", monospace; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid; padding: 4px 8px; }}
blockquote {{ border-left: 3px solid; margin: 0; padding-left: 10px; }}
.msg {{ margin: 10px 0; }}
.user {{ display: flex; justify-content: flex-end; }}
.user .bubble {{ border-radius: 12px; padding: 8px 12px;
        max-width: 78%; white-space: pre-wrap; overflow-wrap: anywhere; }}
.assistant {{ display: block; }}
.usage {{ margin-top: 4px; }}
.tag {{ margin-left: 6px; }}
.error {{ border: 1px solid; border-radius: 6px; padding: 6px 10px; }}
{theme_css}
{css}
</style></head><body>{body}</body></html>"""


def _page(
    theme: str | None, body: str, css: str = "", font_size: str | None = DEFAULT_FONT_SIZE
) -> str:
    return _TEMPLATE.format(theme_css=markdown_css(theme, font_size), css=css, body=body)


def _bubble_user(text: str) -> str:
    return f'<div class="msg user"><div class="bubble">{_html.escape(text or "")}</div></div>'


def _block_assistant(text: str, usage: str | None, interrupted: bool) -> str:
    parts = [f'<div class="msg assistant">{_MD.render(text or "")}</div>']
    meta: list[str] = []
    if usage:
        meta.append(f'<span class="usage">{_html.escape(usage)}</span>')
    if interrupted:
        meta.append('<span class="tag">已中断</span>')
    if meta:
        parts.append("".join(meta))
    return "".join(parts)


def _block_error(message: str, detail: str | None) -> str:
    text = _html.escape(message or "错误")
    if detail:
        text += f'<br><small>{_html.escape(detail)}</small>'
    return f'<div class="msg error">{text}</div>'


def messages_to_html(
    messages: list[dict],
    theme: str | None = DEFAULT_THEME,
    font_size: str | None = DEFAULT_FONT_SIZE,
) -> str:
    """把消息模型列表渲染为整段消息流 HTML。"""
    parts: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            parts.append(_bubble_user(m.get("content", "")))
        elif role == "assistant":
            parts.append(
                _block_assistant(m.get("content", ""), m.get("usage"), bool(m.get("interrupted")))
            )
        elif role == "error":
            parts.append(_block_error(m.get("content", ""), m.get("detail")))
    return _page(theme, "\n".join(parts), _pygments_css(theme), font_size)


_ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")


def markdown_to_html(
    text: str, theme: str | None = DEFAULT_THEME, font_size: str | None = DEFAULT_FONT_SIZE
) -> str:
    return _page(theme, _MD.render(text or ""), _pygments_css(theme), font_size)


def plain_to_html(
    text: str, theme: str | None = DEFAULT_THEME, font_size: str | None = DEFAULT_FONT_SIZE
) -> str:
    return _page(theme, f"<pre>{_html.escape(text or '')}</pre>", "", font_size)


def ansi_to_html(
    text: str, theme: str | None = DEFAULT_THEME, font_size: str | None = DEFAULT_FONT_SIZE
) -> str:
    """极简 ANSI SGR 解析（颜色 / 加粗 / 重置）。"""
    colors = ansi_colors(theme)
    out: list[str] = []
    open_span = False
    pos = 0
    for match in _ANSI_RE.finditer(text or ""):
        out.append(_html.escape(text[pos : match.start()]))
        pos = match.end()
        code = match.group(1)
        if code in colors:
            if open_span:
                out.append("</span>")
            out.append(f'<span style="color:{colors[code]}">')
            open_span = True
        elif code in ("", "0"):
            if open_span:
                out.append("</span>")
                open_span = False
        elif code == "1":
            if open_span:
                out.append("</span>")
            out.append('<span style="font-weight:bold">')
            open_span = True
    out.append(_html.escape(text[pos:]))
    if open_span:
        out.append("</span>")
    return _page(theme, f"<pre>{''.join(out)}</pre>", "", font_size)


def render_to_html(
    kind: str,
    text: str,
    theme: str | None = DEFAULT_THEME,
    font_size: str | None = DEFAULT_FONT_SIZE,
) -> str:
    if kind == "markdown":
        return markdown_to_html(text, theme, font_size)
    if kind == "ansi":
        return ansi_to_html(text, theme, font_size)
    return plain_to_html(text, theme, font_size)
