"""统一渲染管线：把任意可渲染内容转 HTML（docs 05 §3 统一渲染管线）。

- Markdown → HTML（markdown-it-py + Pygments 代码高亮）
- 纯文本 → 转义 HTML
- ANSI 转义（shell 输出）→ 带色 HTML
输出喂给同一 QWebEngineView（本地离线，无外部 CDN）。
"""

from __future__ import annotations

import html as _html
import re

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

_FORMATTER = HtmlFormatter(cssclass="highlight")
_PYGMENTS_CSS = _FORMATTER.get_style_defs(".highlight")


def _highlight(code: str, lang: str, _attrs: str = "") -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
    except ClassNotFound:
        lexer = get_lexer_by_name("text")
    return highlight(code, lexer, _FORMATTER)


_MD = MarkdownIt("commonmark", {"highlight": _highlight}).enable("table").enable("strikethrough")

_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: system-ui, "Segoe UI", sans-serif; font-size: 14px; line-height: 1.6;
        color: #1F2328; background: #FFFFFF; margin: 8px 12px; }}
pre {{ background: #F7F8FA; padding: 8px 10px; border-radius: 6px; overflow-x: auto; }}
code {{ font-family: Consolas, "Courier New", monospace; font-size: 13px; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid #E5E7EB; padding: 4px 8px; }}
blockquote {{ border-left: 3px solid #E5E7EB; margin: 0; padding-left: 10px; color: #6B7280; }}
a {{ color: #2563EB; }}
.msg {{ margin: 10px 0; }}
.user {{ display: flex; justify-content: flex-end; }}
.user .bubble {{ background: #F7F8FA; border-radius: 12px; padding: 8px 12px;
        max-width: 78%; white-space: pre-wrap; }}
.assistant {{ display: block; }}
.usage {{ color: #6B7280; font-size: 12px; margin-top: 4px; }}
.tag {{ color: #6B7280; font-size: 12px; margin-left: 6px; }}
.error {{ background: #FEF2F2; border: 1px solid #FECACA; color: #DC2626;
        border-radius: 6px; padding: 6px 10px; }}
{css}
</style></head><body>{body}</body></html>"""


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


def messages_to_html(messages: list[dict]) -> str:
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
    return _TEMPLATE.format(css=_PYGMENTS_CSS, body="\n".join(parts))

_ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_COLORS = {
    "30": "#1F2328", "31": "#DC2626", "32": "#16A34A", "33": "#D97706",
    "34": "#2563EB", "35": "#9333EA", "36": "#0891B2", "37": "#6B7280",
    "90": "#6B7280", "91": "#DC2626", "92": "#16A34A", "93": "#D97706",
    "94": "#2563EB", "95": "#9333EA", "96": "#0891B2", "97": "#1F2328",
}


def markdown_to_html(text: str) -> str:
    return _TEMPLATE.format(css=_PYGMENTS_CSS, body=_MD.render(text or ""))


def plain_to_html(text: str) -> str:
    body = f"<pre>{_html.escape(text or '')}</pre>"
    return _TEMPLATE.format(css="", body=body)


def ansi_to_html(text: str) -> str:
    """极简 ANSI SGR 解析（颜色 / 加粗 / 重置）。"""
    out: list[str] = []
    open_span = False
    pos = 0
    for match in _ANSI_RE.finditer(text or ""):
        out.append(_html.escape(text[pos : match.start()]))
        pos = match.end()
        code = match.group(1)
        if code in _ANSI_COLORS:
            if open_span:
                out.append("</span>")
            out.append(f'<span style="color:{_ANSI_COLORS[code]}">')
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
    return _TEMPLATE.format(css="", body=f"<pre>{''.join(out)}</pre>")


def render_to_html(kind: str, text: str) -> str:
    if kind == "markdown":
        return markdown_to_html(text)
    if kind == "ansi":
        return ansi_to_html(text)
    return plain_to_html(text)
