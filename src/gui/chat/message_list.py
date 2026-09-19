"""消息流（整段渲染 + 合帧刷新，docs 05 §3）。

单个渲染视图承载整条消息流；流式增量经 50ms 合帧后整帧替换。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget

from gui import theme as theme_module
from gui.theme import DEFAULT_FONT_SIZE, DEFAULT_THEME
from gui.widgets.render import md
from gui.widgets.render.view import RendererView


class MessageList(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._messages: list[dict] = []
        self._theme = DEFAULT_THEME
        self._font_size = DEFAULT_FONT_SIZE
        self._renderer = RendererView(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._renderer)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._render)

    # -- 外观 --------------------------------------------------------------
    def set_theme(self, name: str | None, font_size: str | None = None) -> None:
        """切换外观并整帧重渲染（消息内容不变，仅换色与字号）。"""
        name = name or DEFAULT_THEME
        font_size = font_size or DEFAULT_FONT_SIZE
        if name != self._theme or font_size != self._font_size:
            self._theme = name
            self._font_size = font_size
            self._render()

    # -- 渲染 --------------------------------------------------------------
    def _render(self, jump_bottom: bool = False) -> None:
        inner = md.messages_inner(self._messages, self._theme, self._font_size)
        # 随帧下发主题底色；WebEngine 侧走 innerHTML 局部更新（rev19：不重载、不闪、无 2MB 限）
        # jump_bottom：会话切换/清空后无条件回底（rev21）；流式帧仍只在「原本在底部」时跟底
        self._renderer.set_stream(inner, theme_module.palette(self._theme).bg, jump_bottom)

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _last_is_assistant(self) -> bool:
        return bool(self._messages) and self._messages[-1].get("role") == "assistant"

    # -- 操作 --------------------------------------------------------------
    def is_empty(self) -> bool:
        """消息流是否为空（空状态切换判据）。"""
        return not self._messages

    def clear(self) -> None:
        self._messages.clear()
        self._render(jump_bottom=True)

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})
        self._render()

    def begin_assistant(self) -> None:
        self._messages.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning": "",
                "usage": None,
                "interrupted": False,
            }
        )
        self._render()

    def append_delta(self, text: str) -> None:
        if not self._last_is_assistant():
            self.begin_assistant()
        self._messages[-1]["content"] += text
        self._schedule()

    def append_reasoning(self, text: str) -> None:
        """思考增量（rev25）：写入当前助手消息的 reasoning 段。"""
        if not self._last_is_assistant():
            self.begin_assistant()
        self._messages[-1]["reasoning"] += text
        self._schedule()

    def finalize(
        self, content: str, usage_text: str | None, interrupted: bool, reasoning: str = ""
    ) -> None:
        if not self._last_is_assistant():
            self.begin_assistant()
        self._messages[-1]["content"] = content
        self._messages[-1]["reasoning"] = reasoning
        self._messages[-1]["usage"] = usage_text
        self._messages[-1]["interrupted"] = interrupted
        self._render()

    def add_error(self, message: str, detail: str | None = None) -> None:
        self._messages.append({"role": "error", "content": message, "detail": detail})
        self._render()

    @staticmethod
    def format_usage(usage: dict | None) -> str | None:
        """用量文案（rev24/25）：单次口径 + 平均 TPS + 总耗时。

        TPS 取**生成阶段**（首 token → 末 token），排除排队与预填充，最贴近体感；
        旧数据无计时则只显 tokens（不编造速度）。
        """
        if not usage:
            return None
        total = usage.get("total_tokens")
        if total is None:
            return None
        text = f"本次 {total} tokens"
        completion = usage.get("completion_tokens") or 0
        elapsed_ms = usage.get("elapsed_ms") or 0
        first_ms = usage.get("first_token_ms") or 0
        span_ms = elapsed_ms - first_ms
        if completion and span_ms > 0:
            text += f" · {completion / (span_ms / 1000):.1f} tokens/s"
        if elapsed_ms:
            text += f" · {elapsed_ms / 1000:.1f}s"
        return text

    def scroll_to_user(self, ordinal: int) -> None:
        """滚动到第 ordinal 个用户提问（rev24：右侧问题列表跳转）。"""
        seen = -1
        for i, m in enumerate(self._messages):
            if m.get("role") != "user":
                continue
            seen += 1
            if seen == ordinal:
                self._renderer.scroll_to(i)
                return

    def load_events(self, events: list[dict]) -> None:
        self._messages.clear()
        for event in events:
            t = event.get("type")
            payload = event.get("payload", {})
            if t == "msg.user":
                self._messages.append({"role": "user", "content": payload.get("text", "")})
            elif t == "msg.assistant.final":
                self._messages.append(
                    {
                        "role": "assistant",
                        "content": payload.get("content", ""),
                        "reasoning": payload.get("reasoning", ""),
                        "usage": self.format_usage(payload.get("usage")),
                        "interrupted": bool(payload.get("interrupted")),
                    }
                )
            elif t == "error":
                self._messages.append(
                    {"role": "error", "content": payload.get("message", ""), "detail": payload.get("detail")}
                )
        self._render(jump_bottom=True)  # 会话回放：从最新处看起（rev21）
