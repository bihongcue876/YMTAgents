"""侧栏：图标栏（rail）+ 会话面板（rev2 §1.2 / rev13 响应式布局 / v0.0.6 工作区分组）。

两种折叠语义**正交**，不要混（spec v0.0.6 §3.4.4 C6）：
- **rail 折叠**（`☰ 会话`）：藏起整个会话面板，只留 48px 图标栏；
- **工作区分组折叠**：只藏该组下的会话列表，标题行与计数仍在。

分组数据由两路事件驱动：`SessionIndex`（会话归属）与 `WorkspaceList`（工作区与折叠态）；
两路都到齐才分组，缺一路时退化为平铺列表 —— 首帧不空白、扫描顺序不产生闪烁。
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from shared.ids import WS_DEFAULT

RAIL_PX = 96  # 折叠后侧栏总宽（图标 + 文字，rev18）
PANEL_MIN_PX = 180  # 展开时面板最小宽
PANEL_MAX_PX = 360  # 展开时面板最大宽
RAIL_BTN_W = RAIL_PX - 12  # 按钮宽 = rail 减左右边距
RAIL_BTN_H = 34

#: 组展开/折叠动画时长（毫秒）——太长显得拖沓，太短等于没有（rev58）
ANIM_MS = 180

#: 默认工作区在侧栏的展示名兜底（正常由 `WorkspaceList` 给出）。
DEFAULT_LABEL = "默认工作区"


def _rel_time(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((now - dt).total_seconds())
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


class Sidebar(QWidget):
    new_session = Signal()
    new_session_in = Signal(str)  # workspace_id：在该分组下新建
    resume_session = Signal(str)
    archive_session = Signal(str)
    unarchive_session = Signal(str)
    delete_session = Signal(str)
    move_session = Signal(str, str)  # session_id, workspace_id（""= 默认工作区）
    detail_session = Signal(str)  # rev24：右键「详情」→ 唤起右侧面板
    switch_workspace = Signal(str)  # v0.0.6：设为当前工作区
    collapse_workspace = Signal(str, bool)  # v0.0.6：workspace_id, collapsed
    rename_workspace = Signal(str)
    open_workspace_dir = Signal(str)
    delete_workspace = Signal(str)
    open_models = Signal()
    open_personas = Signal()
    open_skills = Signal()
    open_plugins = Signal()
    open_terminal = Signal()
    open_workspaces = Signal()
    open_settings = Signal()
    toggle_requested = Signal()  # 折叠/展开请求（宽度由 MainWindow 的 QSplitter 落实）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(RAIL_PX + PANEL_MIN_PX)
        self.setMaximumWidth(RAIL_PX + PANEL_MAX_PX)

        # 分组数据缓存（两路事件各自更新，合并重建）
        self._metas: list = []
        self._workspaces: list = []
        self._current_ws: str = WS_DEFAULT
        self._collapsed: set[str] = set()
        self._archived_open = False
        # rev58 动画状态：折叠动画期间数据事件只更新缓存、不重建（动画收尾统一重建）；
        # _anim_ws = 刚请求展开的组，重建时为它播入场动画
        self._animating = False
        self._anim_ws: str | None = None
        self._anims: list[QPropertyAnimation] = []

        # -- 图标栏（折叠后仍保留；rev18：图标右侧带文字，只看图标猜不出功能） --
        # rev55：rail 收进 #railHost（surface 底 + 右分隔线），导航按钮带 checked 态
        def _rail_button(text: str, tooltip: str) -> QPushButton:
            button = QPushButton(text)
            button.setObjectName("railButton")
            button.setToolTip(tooltip)
            button.setFixedSize(RAIL_BTN_W, RAIL_BTN_H)
            button.setCursor(Qt.PointingHandCursor)
            return button

        self._toggle = _rail_button("☰ 会话", "折叠 / 展开会话")
        self._toggle.clicked.connect(self.toggle_requested.emit)

        self._workspaces_btn = _rail_button("🗂 工作区", "工作区（目录 · 记忆落点 · 文件）")
        self._models_btn = _rail_button("⚙ 模型", "模型配置")
        self._personas_btn = _rail_button("🎭 角色", "角色配置（Persona）")
        self._skills_btn = _rail_button("🧩 技能", "技能（提示词指令包）")
        self._plugins_btn = _rail_button("🔌 插件", "MCP 插件与工具")
        self._terminal_btn = _rail_button("⌨ 终端", "本机终端（shell 会话与监视）")
        self._settings_btn = _rail_button("🛠 设置", "系统设置")

        # 导航态注册表（rev55）：key 与 MainWindow 的页序对应，set_active 由页切换驱动
        self._nav: dict[str, tuple[QPushButton, object]] = {
            "workspaces": (self._workspaces_btn, self.open_workspaces),
            "models": (self._models_btn, self.open_models),
            "personas": (self._personas_btn, self.open_personas),
            "skills": (self._skills_btn, self.open_skills),
            "plugins": (self._plugins_btn, self.open_plugins),
            "terminal": (self._terminal_btn, self.open_terminal),
            "settings": (self._settings_btn, self.open_settings),
        }
        for key, (button, signal) in self._nav.items():
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, k=key, s=signal: self._nav_goto(k, s))

        rail = QVBoxLayout()
        rail.setContentsMargins(4, 4, 4, 4)
        rail.setSpacing(4)
        rail.addWidget(self._toggle)
        rail.addStretch(1)
        for button in (self._workspaces_btn, self._models_btn, self._personas_btn,
                       self._skills_btn, self._plugins_btn, self._terminal_btn,
                       self._settings_btn):
            rail.addWidget(button)

        rail_host = QWidget()
        rail_host.setObjectName("railHost")
        rail_host.setLayout(rail)
        rail_host.setFixedWidth(RAIL_PX)

        # -- 会话面板（可折叠） ----------------------------------------------
        self._new = QPushButton("＋ 新对话")
        self._new.setObjectName("primaryButton")  # rev55：关键 CTA 用 accent 实底
        self._new.setFixedHeight(40)
        self._new.clicked.connect(self.new_session.emit)
        # rev58：▾ 选工作区新建 —— 展开时动态填充（工作区列表会增删）
        self._new_menu_btn = QPushButton("▾")
        self._new_menu_btn.setObjectName("sideMenuBtn")
        self._new_menu_btn.setFixedHeight(40)
        self._new_menu_btn.setToolTip("选择工作区新建对话")
        self._new_menu = QMenu(self)
        self._new_menu.aboutToShow.connect(self._fill_new_menu)
        self._new_menu_btn.setMenu(self._new_menu)
        new_row = QHBoxLayout()
        new_row.setSpacing(4)
        new_row.addWidget(self._new, 1)
        new_row.addWidget(self._new_menu_btn)

        self._list = QVBoxLayout()
        self._list.setAlignment(Qt.AlignTop)
        self._list.setSpacing(6)
        holder = QWidget()
        holder.setLayout(self._list)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(holder)

        self._panel = QWidget()
        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(4, 6, 6, 6)
        panel_layout.addLayout(new_row)
        panel_layout.addWidget(scroll, 1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)  # railHost 自带边距与底色（rev55）
        layout.setSpacing(0)
        layout.addWidget(rail_host)
        layout.addWidget(self._panel, 1)

    # -- 折叠 --------------------------------------------------------------
    def panel_visible(self) -> bool:
        return self._panel.isVisible()

    def _nav_goto(self, key: str, signal) -> None:
        """rail 导航点击（rev55）：先落 checked 态再发信号 —— 页切换会再校准一次（幂等）。"""
        self.set_active(key)
        signal.emit()

    def set_active(self, key: str | None) -> None:
        """rail 导航态（rev55）：当前页按钮反白；回对话页（None）全部复位。"""
        for name, (button, _signal) in self._nav.items():
            button.setChecked(name == key)

    def set_panel_visible(self, visible: bool) -> None:
        """只切面板可见性；最小宽随动（RAIL），拖拽宽度由 MainWindow 负责。"""
        self._panel.setVisible(visible)
        if visible:
            self.setMinimumWidth(RAIL_PX + PANEL_MIN_PX)
            self.setMaximumWidth(RAIL_PX + PANEL_MAX_PX)
        else:
            self.setMinimumWidth(RAIL_PX)
            self.setMaximumWidth(RAIL_PX)

    # -- 数据入口 ----------------------------------------------------------
    def update_sessions(self, metas) -> None:
        """`SessionIndex` 到达：更新会话缓存（归属由 `meta.workspace_id` 给出）。"""
        self._metas = list(metas)
        if not self._animating:  # rev58：折叠动画期间只刷缓存，收尾统一重建
            self._rebuild()

    def update_workspaces(self, workspaces: list[dict], current: str, collapsed) -> None:
        """`WorkspaceList` 到达：更新工作区缓存 + 当前工作区 + 折叠态。"""
        self._workspaces = list(workspaces)
        self._current_ws = current or WS_DEFAULT
        self._collapsed = set(collapsed or [])
        if not self._animating:
            self._rebuild()

    def rebuild(self) -> None:
        """按当前字体度量重算行高后重建（字号档位切换后必须调用）。

        样式表字号**不参与** sizeHint，而本面板把行高渲染成固定高度 —— 不重建则切换字号后
        行高停留在旧档（项目已知陷阱的又一处置点）。
        """
        self._rebuild()

    # -- 重建 --------------------------------------------------------------
    def _workspace_meta(self, workspace_id: str | None) -> dict:
        key = workspace_id or WS_DEFAULT
        for item in self._workspaces:
            if item.get("id") == key:
                return item
        return {
            "id": key,
            "name": DEFAULT_LABEL if key == WS_DEFAULT else key,
            "builtin": key == WS_DEFAULT,
            "root_kind": "managed",
            "root": "",
            "data_home": "",
            "missing": False,
            "migratable": True,
            "current": key == self._current_ws,
        }

    def _groups(self) -> list[tuple[str, list]]:
        """按工作区分组（顺序 = 登记表顺序；未登记归属并入默认工作区）。"""
        buckets: dict[str, list] = {}
        for meta in self._metas:
            buckets.setdefault(meta.workspace_id or WS_DEFAULT, []).append(meta)
        ordered: list[tuple[str, list]] = []
        for item in self._workspaces:
            key = item.get("id") or WS_DEFAULT
            ordered.append((key, buckets.pop(key, [])))
        for key, items in buckets.items():  # 登记表里没有的归属（工作区已被移除）→ 兜底盘
            ordered.append((key, items))
        return ordered

    def _rebuild(self) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先摘父再 deleteLater：deleteLater 要等事件循环才真删，
                # 期间 stale 控件仍会被 findChildren 找到（重建型界面必须立刻摘掉）。
                widget.setParent(None)
                widget.deleteLater()

        if not self._metas and not self._workspaces:
            return  # 首帧：等到数据再建，避免先闪一个假列表

        active_ids = {m.id for m in self._metas if getattr(m, "state", "active") != "archived"}
        archived_ids = {m.id for m in self._metas if getattr(m, "state", "active") == "archived"}

        if not self._workspaces:
            # 工作区列表尚未到达：退化为平铺（不空白、不误导）
            self._list.addWidget(self._section_list(self._pick(active_ids), "会话"))
            self._list.addWidget(self._archived_toggle(len(archived_ids)))
            if self._archived_open and archived_ids:
                self._list.addWidget(self._section_list(self._pick(archived_ids), "已归档会话"))
            return

        # 每个**已登记**的工作区都出分组头（哪怕当前 0 个会话）：分组是工作区在侧栏的存在形式，
        # 空工作区若不出现，用户就没法从侧栏对它「在此新建对话」或改设置。
        for key, items in self._groups():
            rows = [m for m in items if m.id in active_ids]
            self._list.addWidget(self._group(key, rows))

        self._list.addWidget(self._archived_toggle(len(archived_ids)))
        if self._archived_open and archived_ids:
            for key, items in self._groups():
                rows = [m for m in items if m.id in archived_ids]
                if rows:  # 归档区是筛选视图：没有归档会话的工作区不出空组
                    self._list.addWidget(self._group(key, rows, archived=True))

    def _pick(self, ids: set[str]) -> list:
        return [m for m in self._metas if m.id in ids]

    def _archived_toggle(self, count: int) -> QWidget:
        button = QPushButton(f"{'▾' if self._archived_open else '▸'} 已归档 ({count})")
        button.setObjectName("wsGroupHeader")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(self._toggle_archived)
        return button

    def _toggle_archived(self) -> None:
        self._archived_open = not self._archived_open
        self._rebuild()

    def _group(self, workspace_id: str, items: list, archived: bool = False) -> QWidget:
        info = self._workspace_meta(workspace_id)
        collapsed = workspace_id in self._collapsed
        arrow = "▸" if collapsed else "▾"
        label = info.get("name") or workspace_id
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QPushButton(f"{arrow} {label}  ({len(items)})")
        header.setObjectName("wsGroupHeader")
        header.setCursor(Qt.PointingHandCursor)
        header.setToolTip(self._group_tooltip(info))
        header.clicked.connect(
            lambda _=False, wid=workspace_id, col=collapsed: self._request_collapse(wid, not col)
        )
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda pos, wid=workspace_id, w=header: self._workspace_menu(wid, w.mapToGlobal(pos))
        )
        # rev58：行尾常驻小 ＋ —— 一键在该工作区新建对话（不必再找右键菜单）
        add_here = QPushButton("＋")
        add_here.setObjectName("wsGroupAdd")
        add_here.setCursor(Qt.PointingHandCursor)
        add_here.setToolTip(f"在「{label}」新建对话")
        add_here.setFixedWidth(26)
        add_here.clicked.connect(lambda _=False, wid=workspace_id: self.new_session_in.emit(wid))
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.setSpacing(2)
        head_row.addWidget(header, 1)
        head_row.addWidget(add_here)
        layout.addLayout(head_row)

        if not collapsed:
            lst = self._section_list(items, label, archived=archived)
            layout.addWidget(lst)
            if workspace_id == self._anim_ws and not archived:
                # 刚请求展开的组：入场动画（0 → 实际行高），不再「整组砸下来」
                self._anim_ws = None
                target = lst.maximumHeight()  # _section_list 已 setFixedHeight，此处取目标值
                lst.setMinimumHeight(0)
                lst.setMaximumHeight(0)
                self._animate_height(lst, target, lambda w=lst, h=target: w.setFixedHeight(h))
        return box

    # -- 展开/折叠动画（rev58） --------------------------------------------
    def _request_collapse(self, workspace_id: str, collapsed: bool) -> None:
        """组头点击。信号**同步**发出（状态权威走主窗/设置，契约不变）：
        - 折叠：先播收起动画，重建推迟到动画收尾（期间数据事件只刷缓存）；
        - 展开：标记 _anim_ws，重建该组时由 _group 播入场动画。
        动画只在面板**可见**时启用 —— 隐藏控件上的动画既无意义，且在离屏测试环境
        触发过 Qt 访问违例（rev58 实测：plugins 页用例后折叠用例必崩），隐藏走即时路径。
        """
        if not collapsed:
            if self.isVisible():
                self._anim_ws = workspace_id
            self.collapse_workspace.emit(workspace_id, False)
            return
        header = self.sender()
        box = header.parent() if isinstance(header, QWidget) else None
        lst = box.findChild(QListWidget) if box is not None else None
        if lst is None or not self.isVisible():
            self.collapse_workspace.emit(workspace_id, True)
            return
        self._animating = True
        self.collapse_workspace.emit(workspace_id, True)  # 同步：主窗随即刷缓存（不重建）
        lst.setMinimumHeight(0)  # setFixedHeight 锁住了 min/max，动画前必须先放 min
        self._animate_height(lst, 0, self._finish_collapse)

    def _finish_collapse(self) -> None:
        self._animating = False
        self._rebuild()

    def _animate_height(self, widget: QWidget, target: int, on_finished=None) -> None:
        anim = QPropertyAnimation(widget, b"maximumHeight", self)
        anim.setDuration(ANIM_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(widget.maximumHeight())
        anim.setEndValue(target)
        if on_finished is not None:
            anim.finished.connect(on_finished)
        anim.finished.connect(lambda a=anim: self._drop_anim(a))
        self._anims.append(anim)  # 持有引用，防 GC 导致动画中止
        anim.start()

    def _drop_anim(self, anim: QPropertyAnimation) -> None:
        if anim in self._anims:
            self._anims.remove(anim)

    def _fill_new_menu(self) -> None:
        """「＋ 新对话」▾ 菜单：动态列出全部工作区（数据即缓存，展开时填充）。"""
        self._new_menu.clear()
        current_name = self._workspace_meta(self._current_ws).get("name") or DEFAULT_LABEL
        first = self._new_menu.addAction(f"当前工作区（{current_name}）")
        first.triggered.connect(lambda _=False: self.new_session.emit())
        self._new_menu.addSeparator()
        for info in self._workspaces:
            wid = info.get("id") or WS_DEFAULT
            name = info.get("name") or DEFAULT_LABEL
            action = self._new_menu.addAction(f"在「{name}」新建")
            action.triggered.connect(lambda _=False, w=wid: self.new_session_in.emit(w))

    @staticmethod
    def _group_tooltip(info: dict) -> str:
        lines = [info.get("name") or "", f"目录：{info.get('root') or '—'}"]
        if info.get("data_home"):
            lines.append(f"记忆与文档：{info.get('data_home')}")
        if info.get("builtin"):
            lines.append("应用内置：不可移除")
        if info.get("missing"):
            lines.append("⚠ 目录已不存在（可能被移动或删除）")
        if info.get("root_kind") == "external" or not info.get("migratable", True):
            lines.append("注：不随数据根一起迁移")
        lines.append("命令行可访问任意路径、可出网（工作区边界不是安全边界）")
        return "\n".join(lines)

    def _section_list(self, items: list, label: str, archived: bool = False) -> QListWidget:
        lst = QListWidget()
        lst.setObjectName("sessionList")  # rev55：透明底药丸行，去嵌套灰盒
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        if not items:
            empty = QListWidgetItem("（暂无会话）")
            empty.setFlags(Qt.NoItemFlags)
            lst.addItem(empty)
        for meta in items:
            item = QListWidgetItem(f"{meta.title}  ·  {_rel_time(meta.updated_at)}")
            item.setData(Qt.UserRole, meta.id)
            item.setToolTip(f"{meta.title}\n工作区：{label}")
            lst.addItem(item)
        lst.itemClicked.connect(self._on_item_clicked)
        lst.setContextMenuPolicy(Qt.CustomContextMenu)
        lst.customContextMenuRequested.connect(
            lambda pos, widget=lst, arch=archived: self._session_menu(widget, pos, arch)
        )
        # 高度按**真实行高**算（样式表字号不参与 sizeHint —— 项目已知陷阱）：
        # 先填条目再量 sizeHintForRow，最后按需裁掉多余空白（否则 QListWidget 在滚动区里会撑满）。
        rows = max(1, lst.count())
        row_h = max(lst.sizeHintForRow(0), lst.fontMetrics().height() + 6)
        lst.setFixedHeight(rows * row_h + 4)
        return lst

    # -- 交互 --------------------------------------------------------------
    def select(self, session_id: str) -> None:
        """按会话 id 选中列表项（分组改造的回归锚点，见 test_gui_workspaces）。"""
        for lst in self.findChildren(QListWidget):
            for i in range(lst.count()):
                item = lst.item(i)
                if item.data(Qt.UserRole) == session_id:
                    lst.setCurrentItem(item)
                    return

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        session_id = item.data(Qt.UserRole)
        if session_id:
            self.resume_session.emit(session_id)

    def _session_menu(self, widget: QListWidget, pos, archived: bool) -> None:
        item = widget.itemAt(pos)
        if item is None:
            return
        session_id = item.data(Qt.UserRole)
        if not session_id:
            return
        menu = QMenu(self)
        detail = menu.addAction("详情")
        move = menu.addMenu("移到工作区")
        move_targets = [
            (info.get("id") or WS_DEFAULT, info.get("name") or WS_DEFAULT)
            for info in self._workspaces
        ] or [(WS_DEFAULT, DEFAULT_LABEL)]
        move_actions = [
            (move.addAction(name), wid) for wid, name in move_targets
        ]
        archive = menu.addAction("恢复" if archived else "归档")
        delete = menu.addAction("删除")
        chosen = menu.exec(widget.mapToGlobal(pos))
        if chosen == detail:
            self.detail_session.emit(session_id)
            return
        for action, workspace_id in move_actions:
            if chosen == action:
                self.move_session.emit(session_id, "" if workspace_id == WS_DEFAULT else workspace_id)
                return
        if chosen == archive:
            (self.unarchive_session if archived else self.archive_session).emit(session_id)
        elif chosen == delete and self._confirm_delete():
            self.delete_session.emit(session_id)

    def _workspace_menu(self, workspace_id: str, global_pos) -> None:
        info = self._workspace_meta(workspace_id)
        menu = QMenu(self)
        set_current = menu.addAction("设为当前工作区")
        new_here = menu.addAction("在此新建对话")
        collapse = menu.addAction("展开" if workspace_id in self._collapsed else "折叠")
        rename = menu.addAction("重命名")
        open_dir = menu.addAction("打开所在目录")
        remove = menu.addAction("移除登记")
        # 默认工作区不可重命名之外的写入；移除一律不可（R3），与宿主层一致
        rename.setEnabled(True)
        remove.setEnabled(not info.get("builtin"))
        chosen = menu.exec(global_pos)
        if chosen == set_current:
            self.switch_workspace.emit(workspace_id)
        elif chosen == new_here:
            self.new_session_in.emit(workspace_id)
        elif chosen == collapse:
            # 展开走 _request_collapse 可获入场动画（sender 非组头时折叠退化为即时）
            self._request_collapse(workspace_id, workspace_id not in self._collapsed)
        elif chosen == rename:
            self.rename_workspace.emit(workspace_id)
        elif chosen == open_dir:
            self.open_workspace_dir.emit(workspace_id)
        elif chosen == remove and self._confirm_remove(info):
            self.delete_workspace.emit(workspace_id)

    def _confirm_delete(self) -> bool:
        return (
            QMessageBox.question(self, "删除会话", "确定删除该会话？历史目录将保留。")
            == QMessageBox.Yes
        )

    def _confirm_remove(self, info: dict) -> bool:
        """移除登记的知情确认（R2）：**必须写明不删磁盘**，否则用户会以为文件没了。"""
        return (
            QMessageBox.question(
                self,
                "移除工作区",
                f"从列表移除「{info.get('name') or ''}」？\n\n"
                "只会移除这条登记，**不会删除磁盘上的任何文件**"
                "（目录与 `.ymtdata` 记忆目录都原样保留）。",
            )
            == QMessageBox.Yes
        )
