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
import json
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

# 结构性排版（与主题无关）放在页面骨架里，只装载一次；
# 主题色与字号（随外观变化）放进 #stream 内的 <style>，随 innerHTML 局部更新（rev19）。
_BASE_CSS = """body { font-family: system-ui, "Segoe UI", sans-serif; line-height: 1.6;
        margin: 8px 12px; overflow-wrap: anywhere; }
pre { padding: 8px 10px; border-radius: 6px; overflow-x: auto; }
code { font-family: Consolas, "Courier New", monospace; }
table { border-collapse: collapse; }
th, td { border: 1px solid; padding: 4px 8px; }
blockquote { border-left: 3px solid; margin: 0; padding-left: 10px; }
.msg { margin: 10px 0; }
.think { border-left: 3px solid rgba(128,128,128,0.5); padding: 4px 10px; margin: 4px 0 8px;
        opacity: 0.85; }
.think summary { cursor: pointer; opacity: 0.75; }
.think .think-body { white-space: pre-wrap; margin-top: 6px; }
.user { display: flex; justify-content: flex-end; }
.user .bubble { border-radius: 12px; padding: 8px 12px;
        max-width: 90%; white-space: pre-wrap; overflow-wrap: anywhere; }
.assistant { display: block; }
.usage { margin-top: 4px; }
.tag { margin-left: 6px; }
.error { border: 1px solid; border-radius: 6px; padding: 6px 10px; }
.tool { margin: 6px 0; }
.tool details { border: 1px solid rgba(128,128,128,0.35); border-radius: 6px; padding: 4px 10px; }
.tool summary { cursor: pointer; opacity: 0.85; }
.tool pre { margin: 6px 0 2px; }
.tool .tool-label { opacity: 0.7; }"""

_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src http: https: data:;">
<style>{base_css}{shell_bg}</style></head><body><div id="stream">{inner}</div></body></html>"""


def _inner(theme: str | None, body: str, css: str, font_size: str | None) -> str:
    """流内容片段（innerHTML 更新的载荷）：<style>（主题色与字号）+ 消息体。"""
    return f"<style>\n{markdown_css(theme, font_size)}\n{css}\n</style>\n{body}"


def assemble(inner: str, bg: str | None = None) -> str:
    """流片段 → 完整文档（QTextBrowser 降级路径与 WebEngine 初始壳共用）。

    rev35：`bg` 为页面底色；写进壳 CSS 后，首帧原生表面即带主题底色，
    不再先露白（暗色下可见的“白闪”）。仅靠 `page.setBackgroundColor` 时，
    壳本体无底色，首帧仍可能透出白底。
    """
    shell_bg = f"\nhtml, body {{ background: {bg}; }}" if bg else ""
    return _TEMPLATE.format(base_css=_BASE_CSS, shell_bg=shell_bg, inner=inner)


def stub_doc(bg: str | None = None) -> str:
    """空壳文档：WebEngine 初始加载用它（恒小于 setHtml 的 2MB data: URL 上限）。"""
    return assemble("", bg)




def _page(
    theme: str | None, body: str, css: str = "", font_size: str | None = DEFAULT_FONT_SIZE
) -> str:
    return assemble(_inner(theme, body, css, font_size))


def _bubble_user(text: str, index: int = 0) -> str:
    return (
        f'<div class="msg user" id="m{index}">'
        f'<div class="bubble">{_html.escape(text or "")}</div></div>'
    )


def _block_assistant(
    text: str,
    usage: str | None,
    interrupted: bool,
    index: int = 0,
    reasoning: str = "",
) -> str:
    """助手消息：思考块（可折叠）+ 正文 + 用量。

    rev25：思考文本用原生 `<details>`（CSP 禁脚本，`<details>` 无需 JS 即可折叠）。
    rev27：**默认折叠**（不带 `open`），无论流式与否都不自动展开；用户可手动展开。
    """
    parts = [f'<div class="msg assistant" id="m{index}">']
    if reasoning:
        parts.append(
            '<details class="think"><summary>思考过程</summary>'
            f'<div class="think-body">{_html.escape(reasoning)}</div></details>'
        )
    parts.append(_MD.render(text or ""))
    parts.append("</div>")
    meta: list[str] = []
    if usage:
        meta.append(f'<span class="usage">{_html.escape(usage)}</span>')
    if interrupted:
        meta.append('<span class="tag">已中断</span>')
    if meta:
        parts.append("".join(meta))
    return "".join(parts)


def _block_error(message: str, detail: str | None, index: int = 0) -> str:
    text = _html.escape(message or "错误")
    if detail:
        text += f'<br><small>{_html.escape(detail)}</small>'
    return f'<div class="msg error" id="m{index}">{text}</div>'


def _block_tool(message: dict, index: int = 0) -> str:
    """工具调用块（v0.0.3 完善）：折叠展示入参与结果；结果未到前显示「进行中」。

    用原生 `<details>`（CSP 禁脚本），默认折叠，点击展开。
    """
    name = _html.escape(str(message.get("name") or "tool"))
    permission = _html.escape(str(message.get("permission") or "confirm"))
    ok = message.get("ok")
    status = "完成" if ok is True else ("失败" if ok is False else "进行中")
    parts = [
        f'<div class="msg tool" id="m{index}">',
        f'<details><summary>工具调用 · {name}（{permission}）· {status}</summary>',
    ]
    args = message.get("args")
    if args:
        parts.append(
            '<div class="tool-label">入参</div>'
            f"<pre>{_html.escape(json.dumps(args, ensure_ascii=False, indent=2))}</pre>"
        )
    output = message.get("output")
    if output:
        parts.append(f'<div class="tool-label">输出</div><pre>{_html.escape(str(output))}</pre>')
    error = message.get("error")
    if error:
        code = _html.escape(str(error.get("code", "")))
        msg = _html.escape(str(error.get("message", "")))
        parts.append(f'<div class="tool-label">错误</div><pre>{code} {msg}</pre>')
    duration = message.get("duration_ms") or 0
    if duration and ok is not None:
        parts.append(f'<div class="tool-label">{int(duration)} ms</div>')
    parts.append("</details></div>")
    return "".join(parts)


def _messages_body(messages: list[dict]) -> str:
    # rev24：每个消息带 `id="m{i}"` 锚点 —— 右侧问题列表点击后 scrollIntoView 跳转。
    parts: list[str] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "user":
            parts.append(_bubble_user(m.get("content", ""), i))
        elif role == "assistant":
            parts.append(
                _block_assistant(
                    m.get("content", ""),
                    m.get("usage"),
                    bool(m.get("interrupted")),
                    i,
                    m.get("reasoning", ""),
                )
            )
        elif role == "error":
            parts.append(_block_error(m.get("content", ""), m.get("detail"), i))
        elif role == "tool":
            parts.append(_block_tool(m, i))
    return "\n".join(parts)


def messages_inner(
    messages: list[dict],
    theme: str | None = DEFAULT_THEME,
    font_size: str | None = DEFAULT_FONT_SIZE,
) -> str:
    """消息流 → innerHTML 片段（rev19：WebEngine 局部更新的载荷）。"""
    return _inner(theme, _messages_body(messages), _pygments_css(theme), font_size)


def markdown_inner(
    text: str, theme: str | None = DEFAULT_THEME, font_size: str | None = DEFAULT_FONT_SIZE
) -> str:
    """单段 Markdown → innerHTML 片段。"""
    return _inner(theme, _MD.render(text or ""), _pygments_css(theme), font_size)


def messages_to_html(
    messages: list[dict],
    theme: str | None = DEFAULT_THEME,
    font_size: str | None = DEFAULT_FONT_SIZE,
) -> str:
    """把消息模型列表渲染为整段消息流 HTML（QTextBrowser 降级路径用）。"""
    return assemble(messages_inner(messages, theme, font_size))


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
