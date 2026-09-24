"""消息流（增量渲染 + 合帧刷新，docs 05 §3）。

单个渲染视图承载整条消息流；流式增量经 50ms 合帧后**按消息粒度就地更新 DOM**
（根治长对话每帧整段重建造成视觉「整窗重建/假重启」）。会话切换 / 清空 / 主题
整帧换色仍走整段重建（rev19 局部更新纪律）。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

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
        self._renderer.copy_requested.connect(self._on_copy)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._renderer)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        # 合帧后就地更新当前流的末条消息（流式助手 delta/reasoning）
        self._timer.timeout.connect(self._paint_last)

    # -- 外观 --------------------------------------------------------------
    def set_theme(self, name: str | None, font_size: str | None = None) -> None:
        """切换外观并整帧重渲染（消息内容不变，仅换色与字号）。"""
        name = name or DEFAULT_THEME
        font_size = font_size or DEFAULT_FONT_SIZE
        if name != self._theme or font_size != self._font_size:
            self._theme = name
            self._font_size = font_size
            self._render()

    def set_copy_buttons(self, on: bool) -> None:
        """复制按钮显示开关（D-3）：只影响渲染层，不动消息数据。"""
        self._renderer.set_copy_buttons(on)

    # -- 渲染 --------------------------------------------------------------
    def _stream_ready(self) -> bool:
        """增量渲染是否安全：WebEngine 壳已加载 **且** 消息流当前可见。

        不可见时（切页 / 空状态 / 离屏）落回整段重建 —— `set_stream` 按 rev12 置脏，
        showEvent 再整段重放，保证隐藏期增量的最终一致性。
        """
        return self._renderer.stream_ready() and self._renderer.isVisible()

    def _paint_new_tail(self, jump_bottom: bool = False) -> None:
        """追加最末一条新消息的 DOM 节点；视图未就绪 / 降级 / 隐藏 → 整段重建。

        首条真实内容建立视图后，新消息（用户 / 错误 / 工具 / 助手骨架）只追加节点，
        已完成旧消息不重渲染。
        """
        if self._stream_ready():
            idx = len(self._messages) - 1
            self._renderer.append_node(md.message_row(self._messages[idx], idx, self._theme))
        else:
            self._render(jump_bottom)

    def _paint_last(self, jump_bottom: bool = False) -> None:
        """就地重渲染最末消息节点（流式助手 delta / reason / finalize / 工具补全）。

        只更新当前这一条，既不重建已完成旧消息，也不重注整条对话。
        """
        if self._stream_ready():
            idx = len(self._messages) - 1
            self._renderer.update_node(idx, md.message_row(self._messages[idx], idx, self._theme))
        else:
            self._render(jump_bottom)

    def _render(self, jump_bottom: bool = False) -> None:
        if not self._messages and not self._renderer.view_created:
            # rev55：空流且视图未建 → 不渲染。首帧 settings.state 与空会话回放都会走这里，
            # 不能为了「渲染空」把 WebEngine 首视图（GPU/渲染子进程，启动 ~1s）提前拉起；
            # 首条真实消息到达时自会按当前外观建视图。
            return
        inner = md.messages_inner(self._messages, self._theme, self._font_size)
        # 随帧下发主题底色；WebEngine 侧走 innerHTML 局部更新（rev19：不重载、不闪、无 2MB 限）
        # jump_bottom：会话切换/清空后无条件回底（rev21）；流式帧仍只在「原本在底部」时跟底
        self._renderer.set_stream(inner, theme_module.palette(self._theme).bg, jump_bottom)

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _last_is_assistant(self) -> bool:
        return bool(self._messages) and self._messages[-1].get("role") == "assistant"

    # -- 复制（v0.0.11 切片 E）：<kind>:<index> -> 文本（不缓存，避免双份真值） ----
    def _on_copy(self, spec: str) -> None:
        text = self._copy_text(spec)
        if text:
            QApplication.clipboard().setText(text)

    def _copy_text(self, spec: str) -> str:
        kind, _, raw = str(spec or "").partition(":")
        try:
            msg = self._messages[int(raw)]
        except (ValueError, IndexError):
            return ""
        if kind in ("msg-md", "msg-raw"):
            return str(msg.get("content") or "")
        if kind == "think-raw":
            return str(msg.get("reasoning") or "")
        if kind == "think-md":
            body = str(msg.get("reasoning") or "")
            return "\n".join("> " + line for line in body.splitlines())
        if kind in ("tool-md", "tool-raw"):
            return md.tool_copy_text(msg, markdown=(kind == "tool-md"))
        return ""

    # -- 操作 --------------------------------------------------------------
    def is_empty(self) -> bool:
        """消息流是否为空（空状态切换判据）。"""
        return not self._messages

    def clear(self) -> None:
        self._messages.clear()
        self._render(jump_bottom=True)

    def add_user(self, text: str, attachments: list[str] | None = None) -> None:
        self._messages.append(
            {"role": "user", "content": text, "attachments": list(attachments or [])}
        )
        self._paint_new_tail()

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
        # 首个 delta 前先建节点骨架；后续 delta/reasoning 由 `_paint_last` 就地更新
        self._paint_new_tail()

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
        # 只就地更新这条助手节点（content / usage / interrupted / thinking / 折叠）
        self._paint_last()

    def add_error(self, message: str, detail: str | None = None) -> None:
        self._messages.append({"role": "error", "content": message, "detail": detail})
        self._paint_new_tail()

    # -- 工具调用（v0.0.3 完善）：折叠块进入消息流，结果到达后就地补全 --------------
    def add_tool_call(self, payload: dict) -> None:
        self._track_tool_call(payload)
        self._paint_new_tail()

    def add_tool_result(self, payload: dict) -> None:
        call_id = payload.get("call_id", "")
        existed = self._find_tool(call_id) is not None
        self._track_tool_result(payload)
        if existed:
            # 结果到达 → 就地补全该工具节点（不新开节点、不重建无关消息）
            idx = self._index_of_tool(call_id)
            if idx is None:
                return  # 理论上不会发生（existed 已保证），防御分支
            if self._stream_ready():
                self._renderer.update_node(
                    idx, md.message_row(self._messages[idx], idx, self._theme)
                )
            else:
                self._render()
        else:
            self._paint_new_tail()

    def _track_tool_call(self, payload: dict) -> None:
        self._messages.append(
            {
                "role": "tool",
                "call_id": payload.get("call_id", ""),
                "name": payload.get("name", ""),
                "args": payload.get("args") or {},
                "permission": payload.get("permission", "confirm"),
                "ok": None,
                "output": None,
                "output_ref": None,
                "error": None,
                "duration_ms": 0,
            }
        )

    def _track_tool_result(self, payload: dict) -> None:
        call_id = payload.get("call_id", "")
        target = self._find_tool(call_id)
        if target is None:
            target = {
                "role": "tool",
                "call_id": call_id,
                "name": payload.get("name", ""),
                "args": {},
                "permission": "confirm",
            }
            self._messages.append(target)
        target["ok"] = bool(payload.get("ok"))
        target["output"] = payload.get("output")
        target["output_ref"] = payload.get("output_ref")
        target["error"] = payload.get("error")
        target["duration_ms"] = payload.get("duration_ms", 0)

    def _find_tool(self, call_id: str) -> dict | None:
        for message in reversed(self._messages):
            if message.get("role") == "tool" and message.get("call_id") == call_id:
                return message
        return None

    def _index_of_tool(self, call_id: str) -> int | None:
        for i, message in enumerate(self._messages):
            if message.get("role") == "tool" and message.get("call_id") == call_id:
                return i
        return None

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
                self._messages.append(
                    {
                        "role": "user",
                        "content": payload.get("text", ""),
                        "attachments": list(payload.get("attachments") or []),
                    }
                )
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
            elif t == "tool.call":
                self._track_tool_call(payload)
            elif t == "tool.result":
                self._track_tool_result(payload)
        self._render(jump_bottom=True)  # 会话回放：从最新处看起（rev21）
