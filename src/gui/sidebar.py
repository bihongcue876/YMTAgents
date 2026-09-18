"""侧栏：图标栏（rail）+ 会话面板（rev2 §1.2 / rev13 响应式布局）。

折叠语义：只藏会话面板，保留 48px 图标栏 —— 新对话、模型配置、系统设置入口
折叠后仍可点，无需悬浮按钮（VS Code / Cherry Studio 同款）。
宽度不再 setFixedWidth(264)，改由 MainWindow 的 QSplitter 管理拖拽。
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

RAIL_PX = 96  # 折叠后侧栏总宽（图标 + 文字，rev18）
PANEL_MIN_PX = 180  # 展开时面板最小宽
PANEL_MAX_PX = 360  # 展开时面板最大宽
RAIL_BTN_W = RAIL_PX - 12  # 按钮宽 = rail 减左右边距
RAIL_BTN_H = 34


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
    resume_session = Signal(str)
    archive_session = Signal(str)
    unarchive_session = Signal(str)
    delete_session = Signal(str)
    open_models = Signal()
    open_personas = Signal()
    open_settings = Signal()
    toggle_requested = Signal()  # 折叠/展开请求（宽度由 MainWindow 的 QSplitter 落实）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(RAIL_PX + PANEL_MIN_PX)
        self.setMaximumWidth(RAIL_PX + PANEL_MAX_PX)

        # -- 图标栏（折叠后仍保留；rev18：图标右侧带文字，只看图标猜不出功能） --
        self._toggle = QPushButton("☰ 侧栏")
        self._toggle.setObjectName("railButton")
        self._toggle.setToolTip("折叠 / 展开侧栏")
        self._toggle.setFixedSize(RAIL_BTN_W, RAIL_BTN_H)
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.clicked.connect(self.toggle_requested.emit)

        self._models_btn = QPushButton("⚙ 模型")
        self._models_btn.setObjectName("railButton")
        self._models_btn.setToolTip("模型配置")
        self._models_btn.setFixedSize(RAIL_BTN_W, RAIL_BTN_H)
        self._models_btn.setCursor(Qt.PointingHandCursor)
        self._models_btn.clicked.connect(self.open_models.emit)

        self._personas_btn = QPushButton("🎭 角色")
        self._personas_btn.setObjectName("railButton")
        self._personas_btn.setToolTip("角色配置（Persona）")
        self._personas_btn.setFixedSize(RAIL_BTN_W, RAIL_BTN_H)
        self._personas_btn.setCursor(Qt.PointingHandCursor)
        self._personas_btn.clicked.connect(self.open_personas.emit)

        self._settings_btn = QPushButton("🛠 设置")
        self._settings_btn.setObjectName("railButton")
        self._settings_btn.setToolTip("系统设置")
        self._settings_btn.setFixedSize(RAIL_BTN_W, RAIL_BTN_H)
        self._settings_btn.setCursor(Qt.PointingHandCursor)
        self._settings_btn.clicked.connect(self.open_settings.emit)

        rail = QVBoxLayout()
        rail.setContentsMargins(0, 0, 0, 0)
        rail.setSpacing(4)
        rail.addWidget(self._toggle)
        rail.addStretch(1)
        rail.addWidget(self._models_btn)
        rail.addWidget(self._personas_btn)
        rail.addWidget(self._settings_btn)

        # -- 会话面板（可折叠） ----------------------------------------------
        self._new = QPushButton("＋ 新对话")
        self._new.setFixedHeight(40)
        self._new.clicked.connect(self.new_session.emit)

        self._active = QListWidget()
        self._active.itemClicked.connect(self._on_active_clicked)
        self._active.setContextMenuPolicy(Qt.CustomContextMenu)
        self._active.customContextMenuRequested.connect(self._active_menu)

        self._arch_toggle = QPushButton("已归档 (0)")
        self._arch_toggle.setCheckable(True)
        self._arch_toggle.toggled.connect(lambda checked: self._archived.setVisible(checked))
        self._archived = QListWidget()
        self._archived.setVisible(False)
        self._archived.itemClicked.connect(self._on_active_clicked)
        self._archived.setContextMenuPolicy(Qt.CustomContextMenu)
        self._archived.customContextMenuRequested.connect(self._arch_menu)

        self._panel = QWidget()
        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.addWidget(self._new)
        panel_layout.addWidget(self._active, 1)
        panel_layout.addWidget(self._arch_toggle)
        panel_layout.addWidget(self._archived)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(rail)
        layout.addWidget(self._panel, 1)

    # -- 折叠 --------------------------------------------------------------
    def panel_visible(self) -> bool:
        return self._panel.isVisible()

    def set_panel_visible(self, visible: bool) -> None:
        """只切面板可见性；最小宽随动（RAIL），拖拽宽度由 MainWindow 负责。"""
        self._panel.setVisible(visible)
        if visible:
            self.setMinimumWidth(RAIL_PX + PANEL_MIN_PX)
            self.setMaximumWidth(RAIL_PX + PANEL_MAX_PX)
        else:
            self.setMinimumWidth(RAIL_PX)
            self.setMaximumWidth(RAIL_PX)

    # -- 列表 --------------------------------------------------------------
    def update_sessions(self, metas) -> None:
        self._active.clear()
        self._archived.clear()
        archived = 0
        for meta in metas:
            item = QListWidgetItem(f"{meta.title}  ·  {_rel_time(meta.updated_at)}")
            item.setData(Qt.UserRole, meta.id)
            item.setToolTip(meta.title)
            if meta.state == "archived":
                self._archived.addItem(item)
                archived += 1
            else:
                self._active.addItem(item)
        self._arch_toggle.setText(f"已归档 ({archived})")

    def select(self, session_id: str) -> None:
        for lst in (self._active, self._archived):
            for i in range(lst.count()):
                item = lst.item(i)
                if item.data(Qt.UserRole) == session_id:
                    lst.setCurrentItem(item)
                    return

    def _on_active_clicked(self, item: QListWidgetItem) -> None:
        self.resume_session.emit(item.data(Qt.UserRole))

    def _active_menu(self, pos) -> None:
        item = self._active.itemAt(pos)
        if item is None:
            return
        session_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        archive = menu.addAction("归档")
        delete = menu.addAction("删除")
        chosen = menu.exec(self._active.mapToGlobal(pos))
        if chosen == archive:
            self.archive_session.emit(session_id)
        elif chosen == delete and self._confirm_delete():
            self.delete_session.emit(session_id)

    def _arch_menu(self, pos) -> None:
        item = self._archived.itemAt(pos)
        if item is None:
            return
        session_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        restore = menu.addAction("恢复")
        delete = menu.addAction("删除")
        chosen = menu.exec(self._archived.mapToGlobal(pos))
        if chosen == restore:
            self.unarchive_session.emit(session_id)
        elif chosen == delete and self._confirm_delete():
            self.delete_session.emit(session_id)

    def _confirm_delete(self) -> bool:
        return (
            QMessageBox.question(self, "删除会话", "确定删除该会话？历史目录将保留。")
            == QMessageBox.Yes
        )
