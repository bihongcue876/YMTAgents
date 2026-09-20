"""会话详情右栏（rev24，用户裁决：不做 Dialog，做可在会话内唤起的侧栏）。

内容：会话状态（轮次/消息/时间/体积）、最近一次上下文用量（**单次**口径）、
本会话策略（名称 / 作用 / 最大上下文 / 模型参数，可开关）与问题列表
（rev30：轮次编号、当前高亮、完整内容、可搜索 / 可折叠，点击跳转）。

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
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from shared.envelope import ContextUsage, SessionBranches, SessionDetailResult

from gui import theme

#: 三态 → 文字后缀（rev35：强化可辨性）。
_TRI_LABEL = {
    Qt.Unchecked: "已关闭",
    Qt.PartiallyChecked: "跟随默认",
    Qt.Checked: "已开启",
}


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


def _branch_label(branch) -> str:
    """分支列表项文案（rev31）。"""
    label = "主干 br0" if branch.id == "br0" else branch.id
    label += f" · {branch.turns} 轮"
    if branch.fork_seq >= 0:
        label += f" · 分叉于 #{branch.fork_seq}"
    if branch.active:
        label += "（当前）"
    return label


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("mutedNote")
    label.setWordWrap(True)
    return label


def _action_row(*buttons: QPushButton) -> QHBoxLayout:
    """按钮按内容宽度左对齐（rev35）：面板变宽后，纵向铺满会让按钮显得过宽。"""
    row = QHBoxLayout()
    for button in buttons:
        row.addWidget(button)
    row.addStretch(1)
    return row


def _shrinkable(spin: QSpinBox | QDoubleSpinBox) -> None:
    """允许数字框被压窄（rev37）：QSpinBox 的最小宽按「最大位数 + 后缀」算，
    如 `2,000,000 tokens` 会把整栏撑到视口之外、右侧被遮挡。
    `Ignored` 横向策略让布局忽略其 sizeHint，只受显式最小宽约束。"""
    spin.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    spin.setMinimumWidth(72)


class SessionPanel(QWidget):
    close_requested = Signal()
    save_requested = Signal(dict)  # {title, note, max_context, params, memory_use/compress/threshold}
    question_selected = Signal(int)  # 第 n 个用户提问（0 起）
    compress_requested = Signal()  # 压缩较早历史为记忆（v0.0.1）
    open_memory_requested = Signal()  # 打开 memory.md（v0.0.1）
    open_memory_history_requested = Signal(int)  # 查看某版旧记忆（只读、不注入）
    revert_requested = Signal(int)  # 退回到此前：该提问的原始序号（rev31）
    branch_requested = Signal(int)  # 从此处分支：该提问的原始序号（rev31）
    branch_switch_requested = Signal(str)  # 切换活动分支（rev31）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session_id: str | None = None
        self._question_texts: list[str] = []

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
        body.addWidget(QLabel("记忆"))
        body.addLayout(self._build_memory())
        body.addWidget(QLabel("本会话策略"))
        body.addLayout(self._build_policy())
        body.addWidget(QLabel("模型参数"))
        body.addLayout(self._build_params())
        self._save = QPushButton("保存本会话设置")
        self._save.clicked.connect(self._on_save)
        body.addLayout(_action_row(self._save))
        body.addWidget(QLabel("分支"))
        body.addLayout(self._build_branches())
        body.addLayout(self._build_questions())
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

    def _build_memory(self) -> QVBoxLayout:
        """记忆（v0.0.1）：状态 + 使用/压缩开关 + 阈值 + 手动触发 + 打开文件 + 旧记忆。"""
        self._memory_state = _muted("尚未建立记忆")
        self._memory_use = self._tri_state(
            "使用记忆", "勾选=注入记忆；取消=不注入；半选=跟随全局默认"
        )
        self._memory_compress = self._tri_state(
            "压缩记忆", "勾选=允许压缩；取消=禁止；半选=跟随全局默认"
        )
        self._memory_auto = self._tri_state(
            "自动压缩", "勾选=占用达阈值时自动压缩；取消=仅手动；半选=跟随全局默认（默认关）"
        )
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
        form.addRow("使用", self._memory_use)
        form.addRow("压缩", self._memory_compress)
        form.addRow("自动", self._memory_auto)
        form.addRow("压缩阈值", threshold_row)
        self._recommended = _muted("推荐范围 —")

        self._compress = QPushButton("压缩记忆")
        self._compress.setToolTip("把较早对话概括为 memory.md（一次模型调用，会预告成本）")
        self._compress.clicked.connect(self.compress_requested.emit)
        self._open_memory = QPushButton("打开记忆文件")
        self._open_memory.clicked.connect(self.open_memory_requested.emit)

        self._memory_history = QListWidget()
        self._memory_history.setToolTip("已归档的旧记忆版本（点击可查看，不注入）")
        self._memory_history.itemClicked.connect(self._on_memory_history_item)
        self._memory_history_hint = _muted("旧记忆版本会显示在此。")

        box = QVBoxLayout()
        box.addWidget(self._memory_state)
        box.addLayout(form)
        box.addWidget(self._recommended)
        box.addWidget(
            _muted("记忆本质是概括；开启「自动」后，占用达到你设定的阈值才允许自动压缩，低于阈值不压。"
                   "聊天记录与 events.jsonl 始终保留。")
        )
        box.addLayout(_action_row(self._compress, self._open_memory))
        box.addWidget(QLabel("旧记忆"))
        box.addWidget(self._memory_history)
        box.addWidget(self._memory_history_hint)
        return box

    @staticmethod
    def _tri_state(label: str, tip: str) -> QCheckBox:
        """三态开关：半选=跟随全局默认，勾选=开，取消=关。

        rev35：文字带状态后缀、按态着色（`gui.theme` 的 `QCheckBox[tri=…]`）——
        原三态的半选外观不易辨认，用户反馈「勾选与否不明显」。
        """
        box = QCheckBox(label)
        box.setTristate(True)
        box.setToolTip(tip)
        box.setProperty("triLabel", label)
        box.stateChanged.connect(lambda _state, b=box: SessionPanel._retri_label(b))
        SessionPanel._retri_label(box)
        return box

    @staticmethod
    def _retri_label(box: QCheckBox) -> None:
        """按当前三态刷新文字后缀与着色属性。"""
        base = box.property("triLabel") or box.text()
        state = box.checkState()
        box.setText(f"{base}（{_TRI_LABEL[state]}）")
        box.setProperty("tri", {Qt.Checked: "on", Qt.Unchecked: "off"}.get(state, "default"))
        theme.restyle(box)

    @staticmethod
    def _set_tri_state(box: QCheckBox, value: bool | None) -> None:
        if value is None:
            box.setCheckState(Qt.PartiallyChecked)
        else:
            box.setCheckState(Qt.Checked if value else Qt.Unchecked)

    @staticmethod
    def _tri_state_value(box: QCheckBox) -> bool | None:
        state = box.checkState()
        if state == Qt.PartiallyChecked:
            return None
        return state == Qt.Checked

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
        _shrinkable(self._max_ctx)
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
        _shrinkable(spin)
        return spin

    def _param_int(self, maximum: int, value: int, step: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.setEnabled(False)
        _shrinkable(spin)
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

    def _build_branches(self) -> QVBoxLayout:
        """分支树（rev31）：点击切换活动分支；不复制历史，只引用 seq。"""
        self._branch_state = _muted("共 1/5 分支")
        self._branch_list = QListWidget()
        self._branch_list.setToolTip("点击切换到该分支；各分支共享同一份 events.jsonl，不复制历史")
        self._branch_list.itemClicked.connect(self._on_branch_item)
        box = QVBoxLayout()
        box.addWidget(self._branch_state)
        box.addWidget(self._branch_list)
        return box

    def _build_questions(self) -> QVBoxLayout:
        """问题列表（rev30/31）：轮次编号 + 当前高亮 + 完整内容 + 可搜索 / 可折叠。

        列表项用 `Qt.UserRole` 存**原始序号**，搜索过滤后仍能正确跳转（不依赖行号）；
        右键提供「退回到此前 / 从此处分支」（rev31 HTTP 语义都在主窗口侧翻译为 seq）。
        """
        self._question_count = _muted("共 0 条")
        self._question_toggle = QPushButton("收起")
        self._question_toggle.setCheckable(True)
        self._question_toggle.setChecked(True)
        self._question_toggle.setFixedWidth(56)
        self._question_toggle.toggled.connect(self._on_toggle_questions)

        head = QHBoxLayout()
        head.addWidget(QLabel("问题列表"))
        head.addWidget(self._question_count)
        head.addStretch(1)
        head.addWidget(self._question_toggle)

        self._question_search = QLineEdit()
        self._question_search.setPlaceholderText("搜索提问…")
        self._question_search.setClearButtonEnabled(True)
        self._question_search.textChanged.connect(lambda _text: self._render_questions())

        self._questions = QListWidget()
        self._questions.setToolTip("点击跳转到该提问；右键可退回到此前 / 从此处分支")
        self._questions.setWordWrap(True)
        self._questions.itemClicked.connect(self._on_question)
        self._questions.setContextMenuPolicy(Qt.CustomContextMenu)
        self._questions.customContextMenuRequested.connect(self._on_question_menu)

        self._question_body = QWidget()
        inner = QVBoxLayout(self._question_body)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(self._question_search)
        inner.addWidget(self._questions)

        box = QVBoxLayout()
        box.addLayout(head)
        box.addWidget(self._question_body)
        return box

    def _on_toggle_questions(self, shown: bool) -> None:
        self._question_body.setVisible(shown)
        self._question_toggle.setText("收起" if shown else "展开")

    def _render_questions(self) -> None:
        """按当前搜索词重绘问题列表；最后一条（当前所在位置）加粗高亮。"""
        needle = self._question_search.text().strip().lower()
        self._questions.clear()
        total = len(self._question_texts)
        shown = 0
        for index, text in enumerate(self._question_texts):
            if needle and needle not in text.lower():
                continue
            shown += 1
            item = QListWidgetItem(f"第 {index + 1} 轮　{text}")
            item.setData(Qt.UserRole, index)
            item.setToolTip(text)
            if index == total - 1:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setToolTip(f"{text}\n（当前所在位置）")
            self._questions.addItem(item)
        self._question_count.setText(
            f"共 {total} 条" + (f"，筛出 {shown} 条" if needle else "")
        )

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

        if result.memory_revision > 0:
            self._memory_state.setText(
                f"记忆 rev {result.memory_revision}：覆盖至第 {result.memory_covered_seq} 条事件，"
                f"{format_tokens(result.memory_tokens)} tokens"
            )
        else:
            self._memory_state.setText("尚未建立记忆")
        self._set_tri_state(self._memory_use, meta.memory_use)
        self._set_tri_state(self._memory_compress, meta.memory_compress)
        self._set_tri_state(self._memory_auto, meta.memory_auto)
        if meta.memory_threshold is None:
            self._threshold_auto.setChecked(True)
            self._threshold.setValue(max(50, min(result.memory_threshold or 90, 99)))
        else:
            self._threshold_auto.setChecked(False)
            self._threshold.setValue(max(50, min(meta.memory_threshold, 99)))
        if result.memory_recommended_max > 0:
            self._recommended.setText(
                f"推荐范围 {format_tokens(result.memory_recommended_min)}–"
                f"{format_tokens(result.memory_recommended_max)} tokens"
            )
        else:
            self._recommended.setText("推荐范围 —（窗口未知）")
        history = getattr(result, "memory_history", None)  # 契约暂未携带；有则填，无则留提示
        self.set_memory_history([int(r) for r in history] if history else [])

        self._set_usage(result.last_usage, window, result.cumulative_tokens)

        self._question_texts = list(questions)
        self._question_search.clear()  # 切会话时重置搜索词
        self._render_questions()

    def set_memory_history(self, revisions: list[int]) -> None:
        """填「旧记忆」列表（只读）：版本号，可查看但不注入上下文。"""
        self._memory_history.clear()
        for revision in revisions:
            item = QListWidgetItem(f"旧记忆 rev {revision}")
            item.setData(Qt.UserRole, int(revision))
            item.setToolTip("该版本已归档，可查看（不注入上下文）")
            self._memory_history.addItem(item)
        self._memory_history_hint.setText(
            "已归档的旧记忆（只读、不注入）" if revisions else "旧记忆版本会显示在此。"
        )

    def _on_memory_history_item(self, item: QListWidgetItem) -> None:
        revision = item.data(Qt.UserRole)
        if revision is not None:
            self.open_memory_history_requested.emit(int(revision))

    def set_memory_error(self, message: str) -> None:
        """压缩失败提示（v0.0.1）：只改状态行，不动记忆文件。"""
        self._memory_state.setText(f"压缩未完成：{message}")

    def set_branches(self, event: SessionBranches) -> None:
        """填充分支树（rev31）：列表 + 上限状态；点击项切换活动分支。"""
        self._branch_list.clear()
        for branch in event.branches:
            item = QListWidgetItem(_branch_label(branch))
            item.setData(Qt.UserRole, branch.id)
            tip = "点击切换到该分支"
            if branch.head_seq >= 0:
                tip += f"\n活动前缀末事件 #{branch.head_seq}"
            item.setToolTip(tip)
            if branch.active:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self._branch_list.addItem(item)
        self._branch_state.setText(f"共 {len(event.branches)}/{event.max_branches} 分支")

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
            # v0.0.1 段名为 "memory"；旧回放可能仍是 "summary"，兼容读取。
            memory_tokens = segments.get("memory", segments.get("summary", 0))
            pct = f"（占窗口 {usage.total / window * 100:.1f}%）" if window else ""
            self._usage_segments.setText(
                f"单次输入 ≈ {format_tokens(input_tokens)} tokens{pct}\n"
                f"　系统 {format_tokens(segments.get('system', 0))} · "
                f"记忆 {format_tokens(memory_tokens)} · "
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
        self._memory_state.setText("尚未建立记忆")
        self._set_tri_state(self._memory_use, None)
        self._set_tri_state(self._memory_compress, None)
        self._set_tri_state(self._memory_auto, None)
        self._threshold_auto.setChecked(True)
        self._threshold.setValue(90)
        self._recommended.setText("推荐范围 —")
        self.set_memory_history([])
        self._branch_list.clear()
        self._branch_state.setText("共 1/5 分支")
        self._question_texts = []
        self._question_search.clear()
        self._render_questions()
        self._set_usage(None, 0, 0)

    # -- 交互 --------------------------------------------------------------
    def _on_question(self, item) -> None:
        ordinal = item.data(Qt.UserRole)
        if ordinal is not None:
            self.question_selected.emit(int(ordinal))

    def _on_question_menu(self, pos) -> None:
        """问题项右键菜单（rev31）：退回到此前 / 从此处分支。"""
        item = self._questions.itemAt(pos)
        if item is None:
            return
        ordinal = item.data(Qt.UserRole)
        if ordinal is None:
            return
        menu = QMenu(self._questions)
        act_revert = menu.addAction("退回到此前")
        act_branch = menu.addAction("从此处分支")
        chosen = menu.exec(self._questions.mapToGlobal(pos))
        if chosen == act_revert:
            self.revert_requested.emit(int(ordinal))
        elif chosen == act_branch:
            self.branch_requested.emit(int(ordinal))

    def _on_branch_item(self, item) -> None:
        branch_id = item.data(Qt.UserRole)
        if branch_id is not None:
            self.branch_switch_requested.emit(str(branch_id))

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
                "memory_use": self._tri_state_value(self._memory_use),
                "memory_compress": self._tri_state_value(self._memory_compress),
                "memory_auto": self._tri_state_value(self._memory_auto),
                "memory_threshold": (
                    None if self._threshold_auto.isChecked() else int(self._threshold.value())
                ),
            }
        )