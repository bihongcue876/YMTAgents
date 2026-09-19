"""会话详情右栏（rev24，用户裁决：不做 Dialog，做可在会话内唤起的侧栏）。

内容：会话状态（轮次/消息/时间/体积）、最近一次上下文用量（**单次**口径）、
本会话策略（名称 / 作用 / 最大上下文 / 模型参数，可开关）与问题列表（点击跳转）。

- 面板不持有 core 对象：只接收 controller 回推的 `SessionDetailResult`，提交 `session.update` 请求。
- 上下文用量区分「单次」与「累计」：单次 = 本次调用重发的全部输入；累计 = 各次之和。
- 模型参数逐项带开关：未启用项不下发（沿用供应商默认，rev20/rev24）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from shared.envelope import ContextUsage, SessionDetailResult


def format_tokens(count: int) -> str:
    """token 数可读化（1,000,000 → 1.00M）。"""
    if count <= 0:
        return "未知"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def format_bytes(count: int) -> str:
    """字节数可读化。"""
    if count <= 0:
        return "0 B"
    for unit, size in (("MB", 1024**2), ("KB", 1024)):
        if count >= size:
            return f"{count / size:.1f} {unit}"
    return f"{count} B"


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("mutedNote")
    label.setWordWrap(True)
    return label


class SessionPanel(QWidget):
    close_requested = Signal()
    save_requested = Signal(dict)  # {title, note, max_context, params, summary_threshold}
    question_selected = Signal(int)  # 第 n 个用户提问（0 起）
    summarize_requested = Signal()  # 压缩较早历史（rev26）
    open_summary_requested = Signal()  # 打开 summary.md（rev26）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session_id: str | None = None

        header = QHBoxLayout()
        title = QLabel("会话详情")
        title.setObjectName("pageTitle")
        close = QPushButton("×")
        close.setFixedWidth(28)
        close.setToolTip("收起详情面板")
        close.clicked.connect(self.close_requested.emit)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close)

        content = QWidget()
        body = QVBoxLayout(content)
        body.addLayout(self._build_status())
        body.addWidget(QLabel("上下文用量"))
        body.addLayout(self._build_usage())
        body.addWidget(QLabel("历史压缩"))
        body.addLayout(self._build_summary())
        body.addWidget(QLabel("本会话策略"))
        body.addLayout(self._build_policy())
        body.addWidget(QLabel("模型参数"))
        body.addLayout(self._build_params())
        self._save = QPushButton("保存本会话设置")
        self._save.clicked.connect(self._on_save)
        body.addWidget(self._save)
        body.addWidget(QLabel("问题列表"))
        self._questions = QListWidget()
        self._questions.setToolTip("点击跳转到该提问")
        self._questions.itemClicked.connect(self._on_question)
        body.addWidget(self._questions, 1)
        body.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setFrameShape(QScrollArea.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(header)
        layout.addWidget(scroll, 1)

    # -- 构建 --------------------------------------------------------------
    def _build_status(self) -> QFormLayout:
        self._role = QLabel("—")
        self._model = QLabel("—")
        self._turns = QLabel("—")
        self._messages = QLabel("—")
        self._created = QLabel("—")
        self._updated = QLabel("—")
        self._size = QLabel("—")
        form = QFormLayout()
        form.addRow("角色", self._role)
        form.addRow("模型", self._model)
        form.addRow("轮次", self._turns)
        form.addRow("消息", self._messages)
        form.addRow("创建", self._created)
        form.addRow("更新", self._updated)
        form.addRow("数据体积", self._size)
        return form

    def _build_usage(self) -> QVBoxLayout:
        self._usage_window = _muted("窗口：—")
        self._usage_segments = _muted("—")
        self._usage_cumulative = _muted("累计消耗：—")
        box = QVBoxLayout()
        box.addWidget(self._usage_window)
        box.addWidget(self._usage_segments)
        box.addWidget(self._usage_cumulative)
        box.addWidget(
            _muted("说明：单次消耗 = 每次调用都会重发的全部历史，因此会随轮次增长；"
                   "累计 = 各次之和。")
        )
        return box

    def _build_summary(self) -> QVBoxLayout:
        """历史压缩（rev26）：状态 + 用户指定阈值 + 手动触发 + 打开摘要文件。"""
        self._summary_state = _muted("尚未压缩")
        self._threshold_auto = QCheckBox("跟随默认阈值")
        self._threshold_auto.setChecked(True)
        self._threshold_auto.toggled.connect(lambda on: self._threshold.setEnabled(not on))
        self._threshold = QSpinBox()
        self._threshold.setRange(50, 99)
        self._threshold.setValue(90)
        self._threshold.setSuffix(" %")
        self._threshold.setEnabled(False)
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(self._threshold_auto)
        threshold_row.addWidget(self._threshold, 1)

        form = QFormLayout()
        form.addRow("压缩阈值", threshold_row)
        self._summarize = QPushButton("压缩历史")
        self._summarize.setToolTip("把较早对话概括为 summary.md（一次模型调用，会预告成本）")
        self._summarize.clicked.connect(self.summarize_requested.emit)
        self._open_summary = QPushButton("打开摘要文件")
        self._open_summary.clicked.connect(self.open_summary_requested.emit)

        box = QVBoxLayout()
        box.addWidget(self._summary_state)
        box.addLayout(form)
        box.addWidget(
            _muted("压缩本质是概括；占用达到你设定的阈值才允许自动压缩，低于阈值不压。"
                   "聊天记录与 events.jsonl 始终保留。")
        )
        box.addWidget(self._summarize)
        box.addWidget(self._open_summary)
        return box

    def _build_policy(self) -> QVBoxLayout:
        self._name = QLineEdit()
        self._name.setPlaceholderText("会话名称")
        self._note = QLineEdit()
        self._note.setPlaceholderText("本会话的作用 / 备注（可选）")

        self._auto = QCheckBox("跟随模型窗口")
        self._auto.setChecked(True)
        self._auto.toggled.connect(lambda on: self._max_ctx.setEnabled(not on))
        self._max_ctx = QSpinBox()
        self._max_ctx.setRange(1024, 2_000_000)
        self._max_ctx.setSingleStep(1024)
        self._max_ctx.setValue(128_000)
        self._max_ctx.setEnabled(False)
        self._max_ctx.setSuffix(" tokens")
        max_row = QHBoxLayout()
        max_row.addWidget(self._auto)
        max_row.addWidget(self._max_ctx, 1)

        form = QFormLayout()
        form.addRow("名称", self._name)
        form.addRow("作用", self._note)
        form.addRow("最大上下文", max_row)
        box = QVBoxLayout()
        box.addLayout(form)
        box.addWidget(
            _muted("默认不设上限（用模型窗口）；输出预留与文件截断按窗口自动派生。")
        )
        return box

    def _param_double(self, maximum: float, value: float, step: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, maximum)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        spin.setValue(value)
        spin.setEnabled(False)
        return spin

    def _param_int(self, maximum: int, value: int, step: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.setEnabled(False)
        return spin

    def _param_row(self, label: str, check: QCheckBox, spin: QWidget) -> QHBoxLayout:
        check.toggled.connect(spin.setEnabled)
        row = QHBoxLayout()
        row.addWidget(check)
        row.addWidget(spin, 1)
        return row

    def _build_params(self) -> QFormLayout:
        self._temperature_on = QCheckBox("temperature")
        self._temperature = self._param_double(2.0, 0.7, 0.1)
        self._top_p_on = QCheckBox("top_p")
        self._top_p = self._param_double(1.0, 1.0, 0.05)
        self._top_k_on = QCheckBox("top_k")
        self._top_k = self._param_int(500, 40, 1)
        self._max_tokens_on = QCheckBox("max_tokens")
        self._max_tokens = self._param_int(200_000, 4096, 256)
        form = QFormLayout()
        form.addRow("采样温度", self._param_row("temperature", self._temperature_on, self._temperature))
        form.addRow("核采样", self._param_row("top_p", self._top_p_on, self._top_p))
        form.addRow("候选数", self._param_row("top_k", self._top_k_on, self._top_k))
        form.addRow("输出上限", self._param_row("max_tokens", self._max_tokens_on, self._max_tokens))
        return form

    # -- 数据填充 ----------------------------------------------------------
    def set_detail(self, result: SessionDetailResult, questions: list[str]) -> None:
        self._session_id = result.session_id
        meta = result.meta
        self._role.setText(meta.persona_name or "YMT 预置")
        self._model.setText(meta.main_model or "全局默认")
        self._turns.setText(str(result.turn_count))
        self._messages.setText(
            f"{result.user_count} 问 / {result.assistant_count} 答"
        )
        self._created.setText(meta.created_at.astimezone().strftime("%Y-%m-%d %H:%M"))
        self._updated.setText(meta.updated_at.astimezone().strftime("%Y-%m-%d %H:%M"))
        self._size.setText(format_bytes(result.data_bytes))

        if not self._name.hasFocus():
            self._name.setText(meta.title)
        if not self._note.hasFocus():
            self._note.setText(meta.note or "")

        window = result.effective_window
        if meta.max_context is None:
            self._auto.setChecked(True)
            if window:
                self._max_ctx.setValue(max(1024, window))
        else:
            self._auto.setChecked(False)
            self._max_ctx.setValue(max(1024, min(meta.max_context, 2_000_000)))

        params = meta.params
        self._temperature_on.setChecked(params.temperature is not None)
        if params.temperature is not None:
            self._temperature.setValue(params.temperature)
        self._top_p_on.setChecked(params.top_p is not None)
        if params.top_p is not None:
            self._top_p.setValue(params.top_p)
        self._top_k_on.setChecked(params.top_k is not None)
        if params.top_k is not None:
            self._top_k.setValue(params.top_k)
        self._max_tokens_on.setChecked(params.max_tokens is not None)
        if params.max_tokens is not None:
            self._max_tokens.setValue(params.max_tokens)

        if result.summary_revision > 0:
            self._summary_state.setText(
                f"已压缩 rev {result.summary_revision}：覆盖至第 {result.summary_covered_seq} 条事件，"
                f"摘要 {format_tokens(result.summary_tokens)} tokens"
            )
        else:
            self._summary_state.setText("尚未压缩")
        if meta.summary_threshold is None:
            self._threshold_auto.setChecked(True)
            self._threshold.setValue(max(50, min(result.summary_threshold or 90, 99)))
        else:
            self._threshold_auto.setChecked(False)
            self._threshold.setValue(max(50, min(meta.summary_threshold, 99)))

        self._set_usage(result.last_usage, window, result.cumulative_tokens)

        self._questions.clear()
        for q in questions:
            self._questions.addItem(q)

    def set_summary_error(self, message: str) -> None:
        """压缩失败提示（rev26）：只改状态行，不动摘要文件。"""
        self._summary_state.setText(f"压缩未完成：{message}")

    def _set_usage(
        self, usage: ContextUsage | None, window: int, cumulative: int
    ) -> None:
        self._usage_window.setText(f"窗口：{format_tokens(window)} tokens")
        if usage is None:
            self._usage_segments.setText("单次消耗：尚无数据（发送一条消息后显示）")
        else:
            segments = usage.segments
            reserve = segments.get("reserve", 0)
            input_tokens = max(0, usage.total - reserve)
            pct = f"（占窗口 {usage.total / window * 100:.1f}%）" if window else ""
            self._usage_segments.setText(
                f"单次输入 ≈ {format_tokens(input_tokens)} tokens{pct}\n"
                f"　系统 {format_tokens(segments.get('system', 0))} · "
                f"摘要 {format_tokens(segments.get('summary', 0))} · "
                f"环境 {format_tokens(segments.get('env', 0))} · "
                f"文件 {format_tokens(segments.get('files', 0))} · "
                f"历史 {format_tokens(segments.get('history', 0))}\n"
                f"　输出预留 {format_tokens(reserve)} tokens"
            )
        self._usage_cumulative.setText(f"累计消耗：{format_tokens(cumulative)} tokens")

    def clear(self) -> None:
        """回到无会话状态。"""
        self._session_id = None
        self._role.setText("—")
        self._model.setText("—")
        self._turns.setText("—")
        self._messages.setText("—")
        self._created.setText("—")
        self._updated.setText("—")
        self._size.setText("—")
        self._name.clear()
        self._note.clear()
        self._summary_state.setText("尚未压缩")
        self._questions.clear()
        self._set_usage(None, 0, 0)

    # -- 交互 --------------------------------------------------------------
    def _on_question(self, item) -> None:
        self.question_selected.emit(self._questions.row(item))

    def _on_save(self) -> None:
        if not self._session_id:
            return
        max_context = None if self._auto.isChecked() else int(self._max_ctx.value())
        params: dict = {}
        if self._temperature_on.isChecked():
            params["temperature"] = round(self._temperature.value(), 2)
        if self._top_p_on.isChecked():
            params["top_p"] = round(self._top_p.value(), 2)
        if self._top_k_on.isChecked():
            params["top_k"] = int(self._top_k.value())
        if self._max_tokens_on.isChecked():
            params["max_tokens"] = int(self._max_tokens.value())
        self.save_requested.emit(
            {
                "title": self._name.text().strip(),
                "note": self._note.text().strip(),
                "max_context": max_context,
                "params": params,
                "summary_threshold": (
                    None if self._threshold_auto.isChecked() else int(self._threshold.value())
                ),
            }
        )