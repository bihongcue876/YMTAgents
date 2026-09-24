"""副思考链页（切片 4b）。

信息结构（spec §3.7）：
- 顶部：子选项（手动/自动档 + 指定槽位；**宿主开关**在「设置 → 附加功能」，关闭即卸载）；
  自动档展开「触发策略」只读说明 + 「改为手动」入口。
- 中部：「运行一次」——走与 `btcm.think` **相同**的工具路径（`btcm.run`），不另开通道。
- 下部：思考流（按 Agent 分段；内部推理默认折叠）+ 最近若干次调用记录（终止原因 / 用量 / 耗时）。

对 Agent 循环而言思考流只是工具内部行为（docs 06 §7），故此处纯展示、不参与决策。
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets import card, section_label, text_fit

_SLOT_LABELS = {
    "main": "主槽位（全局默认）",
    "thinking": "思考槽位",
    "fast": "快速槽位",
    "embedding": "嵌入槽位",
}
_AGENT_LABELS = {
    "creative": "创意",
    "validator": "验证",
    "controller": "总控",
    "meta": "元认知",
}
_EFFORT_LABELS = {"light": "略想", "standard": "通用", "deep": "深层"}
_MODE_LABELS = {"auto": "自动形态", "creative": "纯创造", "validate": "纯验证", "long": "长链思考"}

#: 调用记录保留条数（页面仅作近期观测，不承担历史存储 —— 事件流才是权威）。
HISTORY_LIMIT = 20


class ThinkingPage(QWidget):
    update_requested = Signal(str, str)  # trigger, slot
    run_requested = Signal(str, str, str)  # question, effort, mode

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._ready = False
        self._last_agent = ""

        self._title = QLabel("副思考链")
        self._title.setObjectName("pageTitle")
        self._intro = QLabel(
            "为疑难问题提供多角度生成、验证与反思；可手动调用，也可按策略自动触发。"
        )
        self._intro.setObjectName("mutedNote")
        self._intro.setWordWrap(True)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)

        # -- 触发与模型 ----------------------------------------------------
        options_card, options_box = card()
        options_head = QHBoxLayout()
        options_head.addWidget(section_label("触发与模型"))
        options_head.addStretch(1)
        self._ready_label = QLabel("未就绪")
        self._ready_label.setObjectName("btcmReady")
        self._ready_label.setProperty("btcmReady", False)
        options_head.addWidget(self._ready_label)
        options_box.addLayout(options_head)
        note = QLabel(
            "宿主启停在「设置 → 附加功能」控制。手动档仅响应显式调用；自动档允许模型在判断存在严重矛盾时"
            "发起一次中级思考。"
        )
        note.setObjectName("mutedNote")
        note.setWordWrap(True)
        options_box.addWidget(note)
        self._trigger = QComboBox()
        self._trigger.addItem("手动", "manual")
        self._trigger.addItem("自动", "auto")
        self._slot = QComboBox()
        for value, label in _SLOT_LABELS.items():
            self._slot.addItem(label, value)
        options_form = QFormLayout()
        options_form.setHorizontalSpacing(14)
        options_form.setVerticalSpacing(8)
        options_form.addRow("触发方式", self._trigger)
        options_form.addRow("模型槽位", self._slot)
        options_box.addLayout(options_form)
        body.addWidget(options_card)

        # -- 自动档触发策略（只读，仅自动档可见）-----------------------------
        self._strategy_card, strategy_box = card()
        strategy_box.addWidget(section_label("自动档触发策略（只读）"))
        self._strategy_note = QLabel(
            "已在环境声明中挂载一条明示策略：「当判断问题存在严重矛盾、或需要多角度对抗检验时，"
            "可调用 btcm.think 发起一次中级思考（effort=standard）；其余情况不调用。」"
            "—— 常设许可，非自动轮询，默认极少触发。"
        )
        self._strategy_note.setObjectName("mutedNote")
        self._strategy_note.setWordWrap(True)
        strategy_box.addWidget(self._strategy_note)
        manual_row = QHBoxLayout()
        manual_row.addStretch(1)
        self._to_manual = QPushButton("改为手动")
        self._to_manual.setObjectName("primaryButton")
        manual_row.addWidget(self._to_manual)
        strategy_box.addLayout(manual_row)
        self._strategy_card.setVisible(False)
        body.addWidget(self._strategy_card)

        # -- 运行一次 ------------------------------------------------------
        run_card, run_box = card()
        run_box.addWidget(section_label("运行一次"))
        self._question = QPlainTextEdit()
        self._question.setPlaceholderText("输入需要深入思考的问题…")
        self._question.setFixedHeight(72)
        run_box.addWidget(self._question)
        controls = QHBoxLayout()
        self._effort = QComboBox()
        for value, label in _EFFORT_LABELS.items():
            self._effort.addItem(label, value)
        self._effort.setCurrentIndex(1)
        self._mode = QComboBox()
        for value, label in _MODE_LABELS.items():
            self._mode.addItem(label, value)
        self._run = QPushButton("运行一次")
        self._run.setObjectName("primaryButton")
        controls.addWidget(QLabel("深度"))
        controls.addWidget(self._effort)
        controls.addWidget(QLabel("形态"))
        controls.addWidget(self._mode)
        controls.addStretch(1)
        controls.addWidget(self._run)
        run_box.addLayout(controls)
        body.addWidget(run_card)

        # -- 思考流 --------------------------------------------------------
        stream_card, stream_box = card()
        stream_head = QHBoxLayout()
        stream_head.addWidget(section_label("思考流"))
        stream_head.addStretch(1)
        self._reasoning_toggle = QToolButton()
        self._reasoning_toggle.setText("内部推理")
        self._reasoning_toggle.setCheckable(True)
        self._reasoning_toggle.setArrowType(Qt.RightArrow)
        stream_head.addWidget(self._reasoning_toggle)
        stream_box.addLayout(stream_head)
        self._stream = QPlainTextEdit()
        self._stream.setReadOnly(True)
        self._stream.setPlaceholderText("运行后，创意、验证与总控过程会显示在这里。")
        stream_box.addWidget(self._stream)
        self._reasoning = QPlainTextEdit()
        self._reasoning.setReadOnly(True)
        self._reasoning.setPlaceholderText("内部推理（reasoning）默认折叠，勾选右上「内部推理」查看。")
        self._reasoning.setVisible(False)
        stream_box.addWidget(self._reasoning)
        body.addWidget(stream_card, 1)

        # -- 调用记录 ------------------------------------------------------
        history_card, history_box = card()
        history_box.addWidget(section_label(f"调用记录（最近 {HISTORY_LIMIT} 次）"))
        self._history = QListWidget()
        self._history.setObjectName("sessionList")
        self._history.setMaximumHeight(112)
        history_box.addWidget(self._history)
        body.addWidget(history_card)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._intro)
        layout.addLayout(body, 1)

        self._trigger.currentIndexChanged.connect(self._on_option_changed)
        self._slot.currentIndexChanged.connect(self._on_option_changed)
        self._run.clicked.connect(self._on_run)
        self._to_manual.clicked.connect(self._on_to_manual)
        self._reasoning_toggle.toggled.connect(self._on_reasoning_toggled)
        self.set_state("manual", "thinking", False)

    # -- 主题字号 ----------------------------------------------------------
    def refresh_metrics(self, font_size: str | None = None) -> None:
        """标题（title token）纵向适配：主题字号不参与 sizeHint。"""
        self._title.setMinimumHeight(text_fit.line_height(self._title, "title", font_size))

    # -- 宿主态 ------------------------------------------------------------
    def set_state(self, trigger: str, slot: str, ready: bool) -> None:
        """宿主态回推：子选项 + 就绪（未就绪禁用「运行一次」）。"""
        self._loading = True
        index = self._trigger.findData(trigger)
        if index >= 0:
            self._trigger.setCurrentIndex(index)
        index = self._slot.findData(slot)
        if index >= 0:
            self._slot.setCurrentIndex(index)
        self._loading = False

        self._ready = bool(ready)
        self._ready_label.setText("已就绪" if ready else "未就绪（宿主开关在设置页）")
        self._ready_label.setProperty("btcmReady", self._ready)
        theme.restyle(self._ready_label)
        self._trigger.setEnabled(self._ready)
        self._slot.setEnabled(self._ready)
        self._run.setEnabled(self._ready)
        self._question.setEnabled(self._ready)
        self._effort.setEnabled(self._ready)
        self._mode.setEnabled(self._ready)
        self._strategy_card.setVisible(self._ready and self._trigger.currentData() == "auto")

    # -- 思考流 ------------------------------------------------------------
    def on_delta(self, agent: str, kind: str, text: str) -> None:
        if kind == "reasoning":
            self._reasoning.moveCursor(QTextCursor.End)
            self._reasoning.insertPlainText(text)
            return
        if agent != self._last_agent:
            self._last_agent = agent
            self._stream.appendPlainText(f"\n—— {_AGENT_LABELS.get(agent, agent or '思考')} ——")
        self._stream.moveCursor(QTextCursor.End)
        self._stream.insertPlainText(text)

    def on_iteration(self, iteration: int, verdict: str | None, decision: str | None) -> None:
        parts = [f"[第 {iteration} 轮结束]"]
        if verdict:
            parts.append(f"验证={verdict}")
        if decision:
            parts.append(f"meta={decision}")
        self._stream.appendPlainText(" ".join(parts))

    def on_run_result(self, ok: bool, text: str, usage: dict | None = None,
                      duration_ms: int = 0) -> None:
        header = "\n[运行完成]" if ok else "\n[运行失败]"
        self._stream.appendPlainText(header)
        if text:
            self._stream.appendPlainText(text)
        self._add_record(ok, text, usage or {}, duration_ms)

    # -- 内部 --------------------------------------------------------------
    def _add_record(self, ok: bool, text: str, usage: dict, duration_ms: int) -> None:
        verdict = ""
        termination = ""
        if ok and text:
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                payload = {}
            if isinstance(payload, dict):
                verdict = str(payload.get("verdict") or "")
                termination = str(payload.get("termination_reason") or "")
        tokens = usage.get("total_tokens") or 0
        elapsed = duration_ms or usage.get("elapsed_ms") or 0
        stamp = QTime.currentTime().toString("HH:mm:ss")
        effort = _EFFORT_LABELS.get(self._effort.currentData(), "")
        mode = _MODE_LABELS.get(self._mode.currentData(), "")
        fields = [stamp, f"{effort}/{mode}"]
        if termination:
            fields.append(f"终止={termination}")
        if verdict:
            fields.append(f"判定={verdict}")
        fields.append(f"{tokens} tokens")
        fields.append(f"{elapsed} ms")
        fields.append("完成" if ok else "失败")
        self._history.addItem(" · ".join(fields))
        while self._history.count() > HISTORY_LIMIT:
            self._history.takeItem(0)
        self._history.scrollToBottom()

    def _on_option_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._strategy_card.setVisible(self._ready and self._trigger.currentData() == "auto")
        self.update_requested.emit(self._trigger.currentData(), self._slot.currentData())

    def _on_to_manual(self) -> None:
        self._loading = True
        self._trigger.setCurrentIndex(self._trigger.findData("manual"))
        self._loading = False
        self._strategy_card.setVisible(False)
        self.update_requested.emit("manual", self._slot.currentData())

    def _on_reasoning_toggled(self, checked: bool) -> None:
        self._reasoning.setVisible(checked)
        self._reasoning_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _on_run(self) -> None:
        question = self._question.toPlainText().strip()
        if not question or not self._ready:
            return
        self._stream.clear()
        self._reasoning.clear()
        self._last_agent = ""
        self.run_requested.emit(question, self._effort.currentData(), self._mode.currentData())
