"""对话视图：头条 + 消息流 + 输入区（spec §3.2 / rev2 §1.3）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QStackedWidget, QVBoxLayout, QWidget

from gui.chat.empty_state import EmptyState
from gui.chat.header import ChatHeader
from gui.chat.input_bar import InputBar
from gui.chat.message_list import MessageList


class ChatView(QWidget):
    send_message = Signal(str)
    cancel_turn = Signal()
    switch_model = Signal(str)
    switch_persona = Signal(str)
    rename_session = Signal(str)
    new_session = Signal()
    new_session_in = Signal(str)  # rev58：新建会话指定工作区（header ▾ 菜单）
    add_model = Signal()  # 空状态 CTA：跳模型配置页（rev9 §6）
    toggle_detail = Signal()  # rev24：会话详情右栏

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.header = ChatHeader()
        self.messages = MessageList()
        self.empty = EmptyState()
        self.input = InputBar()

        # rev25：瞬态提示条（如「正在探测思考能力」）——不进入消息流、不落盘
        self._notice = QLabel()
        self._notice.setObjectName("mutedNote")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)

        # 空状态与消息流互斥显示（rev2 §1.3）
        self._stack = QStackedWidget()
        self._stack.addWidget(self.empty)
        self._stack.addWidget(self.messages)

        layout = QVBoxLayout(self)
        layout.addWidget(self.header)
        layout.addWidget(self._stack, 1)
        layout.addWidget(self._notice)
        layout.addWidget(self.input)

        self.header.rename.connect(self.rename_session.emit)
        self.header.switch_model.connect(self.switch_model.emit)
        self.header.switch_persona.connect(self.switch_persona.emit)
        self.header.new_session.connect(self.new_session.emit)
        self.header.new_session_in.connect(self.new_session_in.emit)
        self.header.toggle_detail.connect(self.toggle_detail.emit)
        self.input.send_message.connect(self._on_send)
        self.input.cancel.connect(self.cancel_turn.emit)
        self.empty.add_model.connect(self.add_model.emit)
        self.empty.start_chat.connect(self.input.focus)
        self._current_model: str | None = None
        self._refresh_empty()

    def _refresh_empty(self) -> None:
        """空状态 ⇄ 消息流的**唯一**切换点。"""
        self._stack.setCurrentWidget(self.empty if self.messages.is_empty() else self.messages)

    def _on_send(self, text: str) -> None:
        self.messages.add_user(text)
        self._refresh_empty()
        self.send_message.emit(text)

    def set_models(self, providers, current: str | None) -> None:
        self._current_model = current
        self.header.set_models(providers, current)
        # 空状态文案随「有无供应商」切换：无 → 添加模型；有 → 开始对话
        self.empty.set_has_provider(bool(providers))

    def update_workspaces(self, workspaces: list[dict], current: str) -> None:
        """rev58：工作区列表转发给 header（▾ 新建菜单的数据源）。"""
        self.header.set_workspaces(workspaces, current)

    def set_personas(self, personas, current: str | None) -> None:
        """角色下拉（rev23）：current = 当前会话所用角色（None → 全局默认）。"""
        self.header.set_personas(personas, current)

    def set_title(self, title: str) -> None:
        self.header.set_title(title)

    def set_detail_active(self, active: bool) -> None:
        self.header.set_detail_active(active)

    def set_theme(self, name: str | None, font_size: str | None = None) -> None:
        """外观切换：消息流需整帧重渲染，其余控件由全局 QSS 换肤/重排。"""
        self.messages.set_theme(name, font_size)

    def clear(self) -> None:
        """清空消息流（新建会话时），并回到空状态。"""
        self.messages.clear()
        self._refresh_empty()

    def load_session(self, title: str, events: list[dict]) -> None:
        self.header.set_title(title)
        self.messages.load_events(events)
        self.input.set_generating(False)
        self._refresh_empty()

    # -- 事件处理 ----------------------------------------------------------
    def on_delta(self, event) -> None:
        self.messages.append_delta(event.content)
        self._refresh_empty()

    def on_reasoning(self, event) -> None:
        """思考增量（rev25）：进入当前助手消息的折叠块。"""
        self.messages.append_reasoning(event.content)
        self._refresh_empty()

    def on_final(self, event) -> None:
        usage_text = MessageList.format_usage(event.usage.model_dump())
        model = self._current_model or ""
        if usage_text and model:
            usage_text = f"{usage_text} · {model}"
        self.messages.finalize(event.content, usage_text, event.interrupted, event.reasoning)
        self.input.set_generating(False)
        self._refresh_empty()

    def on_status(self, event) -> None:
        if event.state in ("assembling", "probing", "summarizing", "calling", "gating", "executing"):
            self.input.set_generating(True)
        else:
            self.input.set_generating(False)
        note = event.note if event.state in ("probing", "summarizing") else None
        self._notice.setText(note or "")
        self._notice.setVisible(bool(note))

    def on_tool_call(self, event) -> None:
        """工具调用（v0.0.3 完善）：以折叠块进入消息流。"""
        self.messages.add_tool_call(event.model_dump())
        self._refresh_empty()

    def on_tool_result(self, event) -> None:
        """工具结果：按 call_id 就地补全对应工具块。"""
        self.messages.add_tool_result(event.model_dump())
        self._refresh_empty()

    def on_error(self, event) -> None:
        self.messages.add_error(event.message, event.detail)
        self.input.set_generating(False)
        self._refresh_empty()
