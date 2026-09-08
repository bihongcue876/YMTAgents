"""侧栏：会话列表 + 底部两入口（rev2 §1.2）。"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


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
    open_settings = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(264)

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

        self._models_btn = QPushButton("⚙ 模型配置")
        self._models_btn.clicked.connect(self.open_models.emit)
        self._settings_btn = QPushButton("⚙ 系统设置")
        self._settings_btn.clicked.connect(self.open_settings.emit)

        layout = QVBoxLayout(self)
        layout.addWidget(self._new)
        layout.addWidget(self._active, 1)
        layout.addWidget(self._arch_toggle)
        layout.addWidget(self._archived)
        layout.addWidget(self._models_btn)
        layout.addWidget(self._settings_btn)

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
