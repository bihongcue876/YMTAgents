"""技能页（v0.0.4）：Skill = 带元数据的提示词指令包（docs 07 §4.2）。

- 卡片列表：名称 + 徽标（预置 / 启用 / 权限档 / 来源）+ description；
- 操作：导入目录 / Git 导入 / 刷新；启用停用、更新（有来源时）、打开目录、权限档、删除（预置禁用）；
- 独立页面（用户裁决 2026-09-21：不与 MCP 插件页混排）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

PERMISSIONS = ("safe", "confirm", "restricted")
_PERM_TEXT = {"safe": "自动", "confirm": "确认", "restricted": "禁用"}


class SkillsPage(QWidget):
    toggle_requested = Signal(str, bool)      # id, enabled
    import_requested = Signal(str)            # source（目录路径 / git URL）
    update_requested = Signal(str)            # id
    delete_requested = Signal(str)            # id
    permission_requested = Signal(str, str)   # id, permission
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = QLabel("技能")
        self._title.setObjectName("pageTitle")
        self._hint = QLabel(
            "技能是带元数据的提示词指令包（无代码）：启用后模型会看到技能名，"
            "需要时自行调用读取正文并遵循。只提供指令，不提供能力。"
        )
        self._hint.setWordWrap(True)
        self._hint.setObjectName("mutedLabel")

        imp_dir = QPushButton("导入目录")
        imp_dir.clicked.connect(self._on_import_dir)
        imp_git = QPushButton("Git 导入")
        imp_git.clicked.connect(self._on_import_git)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_requested.emit)

        actions = QHBoxLayout()
        actions.addWidget(imp_dir)
        actions.addWidget(imp_git)
        actions.addWidget(refresh)
        actions.addStretch(1)

        self._list = QVBoxLayout()
        self._list.setAlignment(Qt.AlignTop)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._hint)
        layout.addLayout(actions)
        layout.addLayout(self._list)
        layout.addStretch(1)
        self._skills: list[dict] = []
        self._data_root = ""

    def set_data_root(self, data_root: str) -> None:
        self._data_root = data_root

    def update_skills(self, skills: list[dict]) -> None:
        """重建卡片列表（skill.list 事件）。"""
        self._skills = list(skills)
        while self._list.count():
            item = self._list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._skills:
            empty = QLabel("还没有技能。点「导入目录」选择一个含 SKILL.md 的文件夹，或从 Git 仓库导入。")
            empty.setWordWrap(True)
            empty.setObjectName("mutedLabel")
            self._list.addWidget(empty)
            return
        for info in self._skills:
            self._list.addWidget(self._make_card(info))

    # -- 卡片 --------------------------------------------------------------
    def _make_card(self, info: dict) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(card)

        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{info.get('name', info['id'])}</b>"))
        if info.get("builtin"):
            header.addWidget(self._badge("预置"))
        if info.get("enabled"):
            header.addWidget(self._badge("已启用", ok=True))
        if info.get("error"):
            header.addWidget(self._badge("异常", ok=False))
        header.addStretch(1)
        layout.addLayout(header)

        if info.get("description"):
            desc = QLabel(info["description"])
            desc.setWordWrap(True)
            layout.addWidget(desc)
        if info.get("error"):
            err = QLabel(f"解析失败：{info['error']}")
            err.setWordWrap(True)
            err.setObjectName("mutedLabel")
            layout.addWidget(err)
        if info.get("source"):
            src = QLabel(f"来源：{info['source']}")
            src.setObjectName("mutedLabel")
            layout.addWidget(src)

        actions = QHBoxLayout()
        toggle = QPushButton("停用" if info.get("enabled") else "启用")
        toggle.setEnabled(not info.get("error"))  # 解析失败的技能不可启用
        enabled = not info.get("enabled")
        toggle.clicked.connect(
            lambda _=False, sid=info["id"], en=enabled: self.toggle_requested.emit(sid, en)
        )
        actions.addWidget(toggle)
        if info.get("updateable"):
            update = QPushButton("更新")
            update.clicked.connect(lambda _=False, sid=info["id"]: self.update_requested.emit(sid))
            actions.addWidget(update)
        if info.get("source") or info.get("builtin"):
            open_dir = QPushButton("打开目录")
            open_dir.clicked.connect(lambda _=False, sid=info["id"]: self._open_dir(sid))
            actions.addWidget(open_dir)

        perm = QComboBox()
        for value in PERMISSIONS:
            perm.addItem(f"权限：{_PERM_TEXT[value]}", value)
        current = info.get("permission", "safe")
        if current in PERMISSIONS:
            perm.setCurrentIndex(PERMISSIONS.index(current))
        perm.currentIndexChanged.connect(
            lambda _i, sid=info["id"], box=perm: self.permission_requested.emit(sid, box.currentData())
        )
        actions.addWidget(perm)

        delete = QPushButton("删除")
        delete.setEnabled(not info.get("builtin"))
        delete.clicked.connect(
            lambda _=False, sid=info["id"]: self._confirm_delete(sid)
        )
        actions.addWidget(delete)
        actions.addStretch(1)
        layout.addLayout(actions)
        return card

    @staticmethod
    def _badge(text: str, ok: bool | None = None) -> QLabel:
        badge = QLabel(text)
        badge.setObjectName("keyBadge")
        if ok is not None:
            badge.setProperty("keyStored", ok)
        return badge

    # -- 动作 --------------------------------------------------------------
    def _on_import_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "导入技能", "")
        if path:
            self.import_requested.emit(path)

    def _on_import_git(self) -> None:
        url, ok = QInputDialog.getText(
            self, "Git 导入", "仓库地址（https://… 或本地路径）："
        )
        if ok and url.strip():
            self.import_requested.emit(url.strip())

    def _confirm_delete(self, skill_id: str) -> None:
        if QMessageBox.question(self, "删除技能", "确定删除该技能？其目录将被移除。") == QMessageBox.Yes:
            self.delete_requested.emit(skill_id)

    def _open_dir(self, skill_id: str) -> None:
        path = Path(self._data_root) / "skills" / skill_id if self._data_root else None
        if path and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
