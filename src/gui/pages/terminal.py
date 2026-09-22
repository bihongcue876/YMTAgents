"""终端页（v0.0.5 / docs 01 §6④、docs 05 §4④ 由占位转正）。

形态对齐 MCP 插件页：**顶栏操作 + 卡片列表（状态徽标）+ 二级信息行**，
下方是**监视区**（选中的 shell 实时输出 + 用户可直接输入命令）。

资源纪律（用户要求「不浪费资源、快速启动」）：
- 页面本身随主窗构造（`QPlainTextEdit` 级别的成本），但监视区用惰性创建的
  `RendererView`（WebEngine 首视图是启动大头，rev19 同款策略）；
- 输出只在**页面可见**时重渲染，不可见时只累积环形缓冲；
- 无定时器轮询：列表与输出都由 core 推事件驱动。

安全声明（诚实边界，docs 07 §6.2）：shell 内命令的网络行为**不受**本应用出口白名单约束 ——
界面明示，不制造虚假安全感。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets.render.md import ansi_inner
from gui.widgets.render.view import RendererView

#: 每个 shell 的监视缓冲上限（行 / 字符双保险，防长跑内存膨胀）。
MAX_LINES = 2000
MAX_CHARS = 400_000

_STATE_TEXT = {
    "starting": "启动中",
    "ready": "就绪",
    "busy": "执行中",
    "dead": "已退出",
}


class TerminalPage(QWidget):
    spawn_requested = Signal()
    close_requested = Signal(str)
    input_requested = Signal(str, str)  # (shell_id, command)
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shells: list[dict] = []
        self._max = 0
        self._permission = "confirm"
        self._allow_restricted = False
        self._selected = ""
        self._titles: dict[str, str] = {}
        self._buffers: dict[str, list[str]] = {}
        self._dirty = False
        self._theme: str | None = theme.DEFAULT_THEME
        self._font_size: str | None = theme.DEFAULT_FONT_SIZE

        title = QLabel("终端")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "本机直连的持久 shell：模型经统一工具管线调用（默认逐次确认），"
            "你也可以在这里直接使用。"
            "shell 内命令的网络行为不受本应用出口白名单约束。"
        )
        subtitle.setWordWrap(True)

        self._add = QPushButton("＋ 新建终端")
        self._add.clicked.connect(self.spawn_requested.emit)
        self._refresh = QPushButton("刷新")
        self._refresh.clicked.connect(self.refresh_requested.emit)
        self._summary = QLabel("")
        self._summary.setObjectName("mutedNote")
        self._summary.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(self._add)
        top.addWidget(self._refresh)
        top.addWidget(self._summary, 1)

        self._cards_holder = QWidget()
        self._cards = QVBoxLayout(self._cards_holder)
        self._cards.setContentsMargins(0, 0, 0, 0)
        cards_scroll = QScrollArea()
        cards_scroll.setWidgetResizable(True)
        cards_scroll.setWidget(self._cards_holder)

        # -- 监视区 --
        self._monitor_head = QLabel("监视：未选择终端")
        self._monitor_head.setObjectName("mutedNote")
        self._monitor = RendererView()
        self._input = QLineEdit()
        self._input.setPlaceholderText("选中一个终端后，可在此直接输入命令（Enter 执行）")
        self._input.returnPressed.connect(self._on_send_input)
        self._send = QPushButton("发送")
        self._send.clicked.connect(self._on_send_input)
        input_row = QHBoxLayout()
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._send)

        monitor = QWidget()
        monitor_layout = QVBoxLayout(monitor)
        monitor_layout.setContentsMargins(0, 0, 0, 0)
        monitor_layout.addWidget(self._monitor_head)
        monitor_layout.addWidget(self._monitor, 1)
        monitor_layout.addLayout(input_row)

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(cards_scroll)
        split.addWidget(monitor)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 400])

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(top)
        layout.addWidget(split, 1)

        self._sync_input_enabled()

    # -- 数据 --------------------------------------------------------------
    def set_session_titles(self, titles: dict[str, str]) -> None:
        """会话 id → 标题（卡片上显示「所属会话」，比裸 id 可读）。"""
        self._titles = dict(titles or {})
        self._rebuild()

    def update_shells(self, shells: list[dict], max_shells: int, permission: str,
                      allow_restricted: bool) -> None:
        self._shells = list(shells or [])
        self._max = int(max_shells or 0)
        self._permission = permission or "confirm"
        self._allow_restricted = bool(allow_restricted)
        alive = {s.get("id") for s in self._shells}
        for gone in [sid for sid in self._buffers if sid not in alive]:
            self._buffers.pop(gone, None)
        if self._selected not in alive:
            self._selected = next((s.get("id", "") for s in self._shells), "")
        self._summary.setText(
            f"有效权限档：{self._permission}　·　最多 {self._max} 个　·　"
            f"高危命令：{'已显式启用（逐次确认，卡片标注高危）' if self._allow_restricted else '默认拒绝'}"
        )
        self._rebuild()
        self._sync_input_enabled()
        self._render_monitor()

    def on_output(self, shell_id: str, chunk: str) -> None:
        """core 推送的增量输出：进环形缓冲；页面可见且正看它时才重渲染。"""
        if not chunk:
            return
        lines = self._buffers.setdefault(shell_id, [])
        lines.extend(chunk.splitlines(keepends=True))
        total = sum(len(x) for x in lines)
        if len(lines) > MAX_LINES or total > MAX_CHARS:
            del lines[: max(0, len(lines) - MAX_LINES)]
            while sum(len(x) for x in lines) > MAX_CHARS and len(lines) > 1:
                del lines[0]
        if shell_id == self._selected:
            self._render_monitor()

    # -- 渲染 --------------------------------------------------------------
    def _clear_cards(self) -> None:
        while self._cards.count():
            item = self._cards.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild(self) -> None:
        if not self.isVisible():
            self._dirty = True
            return
        self._clear_cards()
        if not self._shells:
            hint = QLabel(
                "尚未唤醒任何终端。模型调用 shell.exec 时按需启动，或点「＋ 新建终端」手动开一个。"
            )
            hint.setWordWrap(True)
            self._cards.addWidget(hint)
        for shell in self._shells:
            self._cards.addWidget(self._card(shell))
        self._cards.addStretch(1)
        self._dirty = False

    def _card(self, shell: dict) -> QWidget:
        shell_id = str(shell.get("id", ""))
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        head = QLabel(f"<b>{shell_id}</b>　·　{shell.get('kind', '')}")
        head.setTextFormat(Qt.RichText)
        state = str(shell.get("state", "ready"))
        badge = QLabel(_STATE_TEXT.get(state, state))
        badge.setObjectName("shellBadge")
        badge.setProperty("shellState", state)
        theme.restyle(badge)

        session_id = str(shell.get("session") or "")
        session_text = self._titles.get(session_id) or (session_id or "（手动）")
        view = QPushButton("查看")
        view.setEnabled(shell_id != self._selected)
        view.clicked.connect(lambda _=False, sid=shell_id: self._select(sid))
        close = QPushButton("关闭")
        close.clicked.connect(lambda _=False, sid=shell_id: self.close_requested.emit(sid))

        row = QHBoxLayout()
        row.addWidget(head)
        row.addWidget(badge)
        row.addStretch(1)
        row.addWidget(view)
        row.addWidget(close)

        detail = QLabel(
            f"会话：{session_text}　·　PID {shell.get('pid') or '-'}　·　cwd {shell.get('cwd') or '-'}"
        )
        detail.setObjectName("mutedNote")
        detail.setWordWrap(True)
        last = str(shell.get("last_command") or "").strip().replace("\n", " ")
        last_label = QLabel(f"最近命令：{last[:120] if last else '-'}")
        last_label.setObjectName("mutedNote")
        last_label.setWordWrap(True)

        layout = QVBoxLayout(card)
        layout.addLayout(row)
        layout.addWidget(detail)
        layout.addWidget(last_label)
        return card

    def _select(self, shell_id: str) -> None:
        self._selected = shell_id
        self._rebuild()
        self._sync_input_enabled()
        self._render_monitor()

    def _sync_input_enabled(self) -> None:
        usable = bool(self._selected)
        self._input.setEnabled(usable)
        self._send.setEnabled(usable)

    def _render_monitor(self) -> None:
        # 不可见时一律不渲染：这才是「惰性创建」的落点 —— 没打开过终端页，
        # 就不会因为 shell.list 把 WebEngine 视图拉起来（rev19 同款纪律）。
        if not self.isVisible():
            self._dirty = True
            return
        if not self._selected:
            self._monitor_head.setText("监视：未选择终端")
            self._monitor.set_stream(ansi_inner("", self._theme, self._font_size, klass="term"),
                                     bg=theme.palette(self._theme).bg)
            return
        kind = next(
            (s.get("kind", "") for s in self._shells if s.get("id") == self._selected), ""
        )
        self._monitor_head.setText(f"监视：{self._selected} · {kind}")
        text = "".join(self._buffers.get(self._selected, []))
        self._monitor.set_stream(
            ansi_inner(text, self._theme, self._font_size, klass="term"),
            bg=theme.palette(self._theme).bg,
            jump_bottom=True,
        )

    # -- 交互 --------------------------------------------------------------
    def _on_send_input(self) -> None:
        text = self._input.text()
        if not self._selected or not text.strip():
            return
        self._input.clear()
        self.input_requested.emit(self._selected, text)

    def set_theme(self, name: str | None, font_size: str | None) -> None:
        """外观变更 → 监视区整帧重渲染（自渲染内容必须整帧重渲染，rev7 纪律）。"""
        used = theme.palette(name).name
        used_font = theme.font_level(font_size).name
        if used == self._theme and used_font == self._font_size:
            return
        self._theme, self._font_size = used, used_font
        self._render_monitor()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """可见时补齐渲染：卡片每次都重建（几个标签的成本），监视区也补一帧。

        隐藏期只置脏不渲染，是「不浪费资源」的落点；可见时无条件重建，
        保证首帧一定是对的（不依赖「是否收到过事件」这种隐式前提）。
        """
        super().showEvent(event)
        self._rebuild()
        self._render_monitor()
