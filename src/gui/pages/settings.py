"""系统设置页（rev2 §1.5）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets import text_fit

VERSION = "0.0.2"


class SettingsPage(QWidget):
    settings_update = Signal(str, object)  # section, data

    def __init__(self, data_root: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._root = Path(data_root) if data_root else Path(".")
        self._loading = False

        self._title = QLabel("系统设置")
        self._title.setObjectName("pageTitle")  # 字号与字重由 theme.stylesheet 提供

        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(QLabel("外观"))
        layout.addLayout(self._build_appearance())
        # rev24：全局上下文策略取消 → 改为「每会话」设置，此处只留指引
        self._context_note = QLabel(
            "上下文策略已改为按对话独立设置：在对话右侧「详情」面板中调整本会话的"
            "最大上下文与模型参数（默认跟随模型窗口，不设上限）。"
        )
        self._context_note.setObjectName("mutedNote")
        self._context_note.setWordWrap(True)
        layout.addWidget(self._context_note)
        layout.addWidget(QLabel("网络白名单"))
        layout.addLayout(self._build_whitelist())
        layout.addWidget(QLabel("数据"))
        layout.addLayout(self._build_data())
        layout.addWidget(QLabel("日志与诊断"))
        layout.addLayout(self._build_logging())
        layout.addWidget(QLabel("关于"))
        layout.addWidget(QLabel(f"言明通 / YMTAgents　版本 {VERSION}"))
        layout.addStretch(1)
        self.refresh_metrics()

    # -- 分区构建（rev22：原 111 行构造器按分区拆开，组装顺序即页面顺序） -----
    def _build_appearance(self) -> QFormLayout:
        """主题 + 字号。"""
        self._theme = QComboBox()
        for name in theme.theme_names():
            self._theme.addItem(theme.palette(name).label, name)
        self._theme.currentIndexChanged.connect(self._on_theme_changed)
        self._font_size = QComboBox()
        for name in theme.font_size_names():
            self._font_size.addItem(theme.font_level(name).label, name)
        self._font_size.currentIndexChanged.connect(self._on_font_size_changed)
        form = QFormLayout()
        form.addRow("主题", self._theme)
        form.addRow("字号", self._font_size)
        return form

    def _build_whitelist(self) -> QVBoxLayout:
        """白名单列表 + 添加/删除行。"""
        self._whitelist = QListWidget()
        self._rule = QLineEdit()
        self._rule.setPlaceholderText("api.example.com 或 *.example.com")
        add_rule = QPushButton("添加")
        add_rule.clicked.connect(self._add_rule)
        del_rule = QPushButton("删除选中")
        del_rule.clicked.connect(self._remove_rule)
        row = QHBoxLayout()
        row.addWidget(self._rule, 1)
        row.addWidget(add_rule)
        row.addWidget(del_rule)
        box = QVBoxLayout()
        box.addWidget(self._whitelist)
        box.addLayout(row)
        return box

    def _build_data(self) -> QVBoxLayout:
        """数据目录 + 备份说明。

        长路径：wordWrap + Ignored 双管齐下 —— wordWrap 只解决换行，
        minimumSizeHint 仍按最长不可断词计（长路径可达 900px+），Ignored 才真正放开下限。
        """
        self._data_path = QLabel(str(self._root))
        self._data_path.setObjectName("dataPathLabel")
        self._data_path.setWordWrap(True)
        self._data_path.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row = QHBoxLayout()
        row.addWidget(self._data_path, 1)
        open_data = QPushButton("打开目录")
        open_data.clicked.connect(lambda: self._open(self._root))
        row.addWidget(open_data)
        self._backup_note = QLabel("备份：配置文件改写前保留同目录 .bak 单代（docs 03 §10.2）。")
        self._backup_note.setObjectName("backupNote")
        self._backup_note.setWordWrap(True)
        self._backup_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        box = QVBoxLayout()
        box.addLayout(row)
        box.addWidget(self._backup_note)
        return box

    def _build_logging(self) -> QHBoxLayout:
        """日志级别 + 打开日志目录。"""
        self._level = QComboBox()
        for level in ("DEBUG", "INFO", "WARN", "ERROR"):
            self._level.addItem(level)
        self._level.setCurrentText("INFO")
        self._level.currentTextChanged.connect(self._on_level_changed)
        open_logs = QPushButton("打开日志目录")
        open_logs.clicked.connect(lambda: self._open(self._root / "logs"))
        row = QHBoxLayout()
        row.addWidget(QLabel("日志级别"))
        row.addWidget(self._level)
        row.addWidget(open_logs)
        row.addStretch(1)
        return row

    # -- 外观（rev16） ------------------------------------------------------
    def refresh_metrics(self, font_size: str | None = None) -> None:
        """页标题（title token）的纵向适配：主题字号不参与 sizeHint。"""
        self._title.setMinimumHeight(text_fit.line_height(self._title, "title", font_size))

    # -- 外观 --------------------------------------------------------------
    def _on_theme_changed(self, index: int) -> None:
        if self._loading:
            return
        name = self._theme.itemData(index)
        if name:
            self.settings_update.emit("ui", {"theme": name})

    def _on_font_size_changed(self, index: int) -> None:
        if self._loading:
            return
        name = self._font_size.itemData(index)
        if name:
            self.settings_update.emit("ui", {"font_size": name})

    # -- 白名单 ------------------------------------------------------------
    def load_settings(self, data: dict) -> None:
        self._loading = True
        ui = data.get("ui", {})
        theme_index = self._theme.findData(ui.get("theme", theme.DEFAULT_THEME))
        self._theme.setCurrentIndex(theme_index if theme_index >= 0 else 0)
        font_index = self._font_size.findData(ui.get("font_size", theme.DEFAULT_FONT_SIZE))
        self._font_size.setCurrentIndex(font_index if font_index >= 0 else 0)
        self._level.setCurrentText(data.get("logging", {}).get("level", "INFO"))
        self._whitelist.clear()
        for rule in data.get("network", {}).get("whitelist", []):
            self._whitelist.addItem(rule)
        self._loading = False

    def _on_level_changed(self, value: str) -> None:
        if not self._loading:
            self.settings_update.emit("logging", {"level": value})

    def _rules(self) -> list[str]:
        return [self._whitelist.item(i).text() for i in range(self._whitelist.count())]

    def _push_whitelist(self) -> None:
        self.settings_update.emit("network", {"whitelist": self._rules()})

    def _add_rule(self) -> None:
        rule = self._rule.text().strip()
        if rule:
            self._whitelist.addItem(rule)
            self._rule.clear()
            self._push_whitelist()

    def _remove_rule(self) -> None:
        for item in self._whitelist.selectedItems():
            self._whitelist.takeItem(self._whitelist.row(item))
        self._push_whitelist()

    @staticmethod
    def _open(path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
