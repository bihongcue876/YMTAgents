"""系统设置页（rev2 §1.5）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets import card, section_label, text_fit
from gui.widgets.switch import Switch

VERSION = "0.0.8"

#: 附加功能显示名（切片 0）；未知 name 回退原名。
_FEATURE_LABELS = {
    "mcp": "MCP 工具",
    "shell": "Shell 终端",
    "skills": "技能（Skills）",
    "btcm": "副思考链（BTCM）",
    "dpim": "小图书馆（DPIM）",
    "retrieval": "检索",
}
_FEATURE_DESCRIPTIONS = {
    "mcp": "连接外部 MCP 服务器并使用其工具",
    "shell": "为 Agent 提供本地终端命令工具",
    "skills": "按需向模型提供技能指令",
    "btcm": "对疑难问题进行副思考与验证",
    "dpim": "管理本地书库、记录外部对话并查看来源关系",
}
_FEATURE_STATES = {
    "ready": "已就绪",
    "degraded": "部分可用",
    "error": "运行异常",
    "disabled": "已关闭",
    "unavailable": "暂未接入",
}


class SettingsPage(QWidget):
    settings_update = Signal(str, object)  # section, data
    feature_toggle = Signal(str, bool)  # name, enabled（切片 0 滑动开关）

    def __init__(self, data_root: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._root = Path(data_root) if data_root else Path(".")
        self._loading = False
        self._loading_features = False
        self._feature_rows: dict[str, tuple[QWidget, QLabel, QLabel, QLabel, Switch]] = {}


        self._title = QLabel("系统设置")
        self._title.setObjectName("pageTitle")  # 字号与字重由 theme.stylesheet 提供

        # rev57：五个分区各入一张卡片 —— 模块分隔靠容器，不再靠空白与小字标题硬挤
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)

        appearance_card, appearance_box = card()
        appearance_box.addWidget(section_label("外观"))
        appearance_box.addLayout(self._build_appearance())
        body.addWidget(appearance_card)

        features_card, features_box = card()
        features_box.addWidget(section_label("附加功能"))
        note = QLabel(
            "滑动开关控制各附加功能的启停：关闭 = 真卸载（不注册工具、无后台活动）；"
            "开启 = 按需装配。尚未接入的模块会显示为禁用状态。"
        )
        note.setObjectName("mutedNote")
        note.setWordWrap(True)
        features_box.addWidget(note)
        self._features_box = QVBoxLayout()
        self._features_box.setContentsMargins(0, 0, 0, 0)
        self._features_box.setSpacing(0)
        features_box.addLayout(self._features_box)
        body.addWidget(features_card)

        # rev24：全局上下文策略取消 → 改为「每会话」设置，此处只留指引
        self._context_note = QLabel(
            "上下文策略已改为按对话独立设置：在对话右侧「详情」面板中调整本会话的"
            "最大上下文与模型参数（默认跟随模型窗口，不设上限）。"
        )
        self._context_note.setObjectName("mutedNote")
        self._context_note.setWordWrap(True)
        body.addWidget(self._context_note)

        whitelist_card, whitelist_box = card()
        whitelist_box.addWidget(section_label("网络白名单"))
        whitelist_box.addLayout(self._build_whitelist())
        body.addWidget(whitelist_card)

        data_card, data_box = card()
        data_box.addWidget(section_label("数据"))
        data_box.addLayout(self._build_data())
        body.addWidget(data_card)

        logging_card, logging_box = card()
        logging_box.addWidget(section_label("日志与诊断"))
        logging_box.addLayout(self._build_logging())
        body.addWidget(logging_card)

        about_card, about_box = card()
        about_box.addWidget(section_label("关于"))
        about_box.addWidget(QLabel(f"言明通 / YMTAgents　版本 {VERSION}"))
        body.addWidget(about_card)
        body.addStretch(1)

        holder = QWidget()
        holder.setLayout(body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(holder)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(scroll, 1)
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
        self._copy_buttons = QCheckBox("显示复制按钮")
        self._copy_buttons.setChecked(True)
        self._copy_buttons.toggled.connect(self._on_copy_buttons_toggled)
        form = QFormLayout()
        form.addRow("主题", self._theme)
        form.addRow("字号", self._font_size)
        form.addRow("复制按钮", self._copy_buttons)
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

    def _on_copy_buttons_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self.settings_update.emit("ui", {"copy_buttons": bool(checked)})

    # -- 白名单 ------------------------------------------------------------
    def load_settings(self, data: dict) -> None:
        self._loading = True
        ui = data.get("ui", {})
        theme_index = self._theme.findData(ui.get("theme", theme.DEFAULT_THEME))
        self._theme.setCurrentIndex(theme_index if theme_index >= 0 else 0)
        font_index = self._font_size.findData(ui.get("font_size", theme.DEFAULT_FONT_SIZE))
        self._font_size.setCurrentIndex(font_index if font_index >= 0 else 0)
        self._copy_buttons.setChecked(bool(ui.get("copy_buttons", True)))
        self._level.setCurrentText(data.get("logging", {}).get("level", "INFO"))
        self._whitelist.clear()
        for rule in data.get("network", {}).get("whitelist", []):
            self._whitelist.addItem(rule)
        self._loading = False

    def set_features(self, features: list[dict]) -> None:
        """按 `feature.state` 更新附加功能行；复用控件，避免状态回推时整卡重建。"""
        self._loading_features = True
        try:
            items = [item for item in (features or []) if item.get("name")]
            names = {str(item["name"]) for item in items}

            # 暂时摘出布局项；仍存在的行控件复用，只有清单中消失的行才销毁。
            while self._features_box.count():
                self._features_box.takeAt(0)
            for name in set(self._feature_rows) - names:
                row, _label, _description, _badge, _switch = self._feature_rows.pop(name)
                row.setParent(None)
                row.deleteLater()

            for info in items:
                name = str(info["name"])
                row_parts = self._feature_rows.get(name)
                if row_parts is None:
                    row = QWidget()
                    row.setObjectName("featureRow")
                    row_layout = QHBoxLayout(row)
                    row_layout.setContentsMargins(8, 7, 8, 7)
                    row_layout.setSpacing(12)

                    copy = QVBoxLayout()
                    copy.setContentsMargins(0, 0, 0, 0)
                    copy.setSpacing(2)
                    heading = QHBoxLayout()
                    heading.setContentsMargins(0, 0, 0, 0)
                    heading.setSpacing(8)
                    label = QLabel()
                    label.setObjectName("featureName")
                    heading.addWidget(label)
                    badge = QLabel()
                    badge.setObjectName("featureState")
                    heading.addWidget(badge)
                    heading.addStretch(1)
                    description = QLabel()
                    description.setObjectName("featureDescription")
                    description.setWordWrap(True)
                    copy.addLayout(heading)
                    copy.addWidget(description)

                    switch = Switch()
                    switch.setObjectName("featureSwitch")
                    switch.setProperty("featureName", name)
                    switch.toggled.connect(
                        lambda checked, n=name: self._on_feature_toggled(n, checked)
                    )
                    switch.setAccessibleName(f"{_FEATURE_LABELS.get(name, name)} 启用")
                    row_layout.addLayout(copy, 1)
                    row_layout.addWidget(switch)
                    row_parts = (row, label, description, badge, switch)
                    self._feature_rows[name] = row_parts

                row, label, description, badge, switch = row_parts
                label.setText(_FEATURE_LABELS.get(name, name))
                description.setText(_FEATURE_DESCRIPTIONS.get(name, ""))
                state = str(info.get("state", "disabled"))
                available = bool(info.get("available", True))
                enabled = bool(info.get("enabled", False))
                if not available:
                    state_text = _FEATURE_STATES["unavailable"]
                    switch.setToolTip("此模块尚未接入，当前不可启用。")
                else:
                    state_text = _FEATURE_STATES.get(state, state)
                    if enabled and state == "disabled":
                        state_text = "尚未就绪"
                    switch.setToolTip("关闭将卸载此功能；开启将按需装配。")
                badge.setText(state_text)
                badge.setProperty("featureState", state)
                badge.setProperty("featureName", name)
                theme.restyle(badge)
                switch.setEnabled(available)
                switch.setChecked(enabled)
                self._features_box.addWidget(row)
                row.show()
        finally:
            self._loading_features = False


    def _on_feature_toggled(self, name: str, checked: bool) -> None:
        if self._loading_features:
            return
        self.feature_toggle.emit(name, bool(checked))

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
