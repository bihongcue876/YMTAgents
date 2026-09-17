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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gui import theme

VERSION = "0.0.0"


class SettingsPage(QWidget):
    settings_update = Signal(str, object)  # section, data

    def __init__(self, data_root: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._root = Path(data_root) if data_root else Path(".")

        title = QLabel("系统设置")
        title.setObjectName("pageTitle")  # 字号与字重由 theme.stylesheet 提供
        self._loading = False

        # 外观（主题 + 字号）
        self._theme = QComboBox()
        for name in theme.theme_names():
            self._theme.addItem(theme.palette(name).label, name)
        self._theme.currentIndexChanged.connect(self._on_theme_changed)
        self._font_size = QComboBox()
        for name in theme.font_size_names():
            self._font_size.addItem(theme.font_level(name).label, name)
        self._font_size.currentIndexChanged.connect(self._on_font_size_changed)
        appearance_form = QFormLayout()
        appearance_form.addRow("主题", self._theme)
        appearance_form.addRow("字号", self._font_size)

        # 上下文策略
        self._history = QSpinBox()
        self._history.setRange(1, 200)
        self._history.setValue(20)
        self._reserve = QSpinBox()
        self._reserve.setRange(0, 1_000_000)
        self._reserve.setValue(4096)
        self._truncate = QSpinBox()
        self._truncate.setRange(0, 10_000_000)
        self._truncate.setValue(8192)
        context_form = QFormLayout()
        context_form.addRow("历史保留轮数", self._history)
        context_form.addRow("输出预留（reserve）", self._reserve)
        context_form.addRow("挂载文件截断上限", self._truncate)
        context_apply = QPushButton("应用上下文策略")
        context_apply.clicked.connect(self._apply_context)

        # 网络白名单
        self._whitelist = QListWidget()
        self._rule = QLineEdit()
        self._rule.setPlaceholderText("api.example.com 或 *.example.com")
        add_rule = QPushButton("添加")
        add_rule.clicked.connect(self._add_rule)
        del_rule = QPushButton("删除选中")
        del_rule.clicked.connect(self._remove_rule)
        wl_row = QHBoxLayout()
        wl_row.addWidget(self._rule, 1)
        wl_row.addWidget(add_rule)
        wl_row.addWidget(del_rule)

        # 数据
        data_row = QHBoxLayout()
        data_row.addWidget(QLabel(str(self._root)))
        open_data = QPushButton("打开目录")
        open_data.clicked.connect(lambda: self._open(self._root))
        data_row.addWidget(open_data)
        backup_note = QLabel("备份：配置文件改写前保留同目录 .bak 单代（docs 03 §10.2）。")

        # 日志
        self._level = QComboBox()
        for level in ("DEBUG", "INFO", "WARN", "ERROR"):
            self._level.addItem(level)
        self._level.setCurrentText("INFO")
        self._level.currentTextChanged.connect(self._on_level_changed)
        open_logs = QPushButton("打开日志目录")
        open_logs.clicked.connect(lambda: self._open(self._root / "logs"))
        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("日志级别"))
        log_row.addWidget(self._level)
        log_row.addWidget(open_logs)
        log_row.addStretch(1)

        # 关于
        about = QLabel(f"言明通 / YMTAgents　版本 {VERSION}")

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(QLabel("外观"))
        layout.addLayout(appearance_form)
        layout.addWidget(QLabel("上下文策略"))
        layout.addLayout(context_form)
        layout.addWidget(context_apply)
        layout.addWidget(QLabel("网络白名单"))
        layout.addWidget(self._whitelist)
        layout.addLayout(wl_row)
        layout.addWidget(QLabel("数据"))
        layout.addLayout(data_row)
        layout.addWidget(backup_note)
        layout.addWidget(QLabel("日志与诊断"))
        layout.addLayout(log_row)
        layout.addWidget(QLabel("关于"))
        layout.addWidget(about)
        layout.addStretch(1)

    # -- 上下文 ------------------------------------------------------------
    def _apply_context(self) -> None:
        self.settings_update.emit(
            "context",
            {
                "history_turns": self._history.value(),
                "reserve": self._reserve.value(),
                "file_truncate": self._truncate.value(),
            },
        )

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
        context = data.get("context", {})
        self._history.setValue(int(context.get("history_turns", 20)))
        self._reserve.setValue(int(context.get("reserve", 4096)))
        self._truncate.setValue(int(context.get("file_truncate", 8192)))
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
