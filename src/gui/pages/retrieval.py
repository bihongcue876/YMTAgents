"""检索页（rev68；用户裁决 2026-09-25 二次：独立一页，不与设置页黏连）。

布局：工具组（search.web / search.fetch，各一开关）+ 引擎组（六家搜索，逐项开关 +
端点展示 + key 填入 + 「测试」连通性检测）。全部经 bus 信封与 core 通信；key 只存
本机加密库、界面不回显；测试结果只读展示（R1–R5，证据过 redact）。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets import card, section_label, text_fit
from gui.widgets.switch import Switch

#: 引擎显示名（webfetch 是抓取语义，不在此列 —— 由工具区的网页抓取承担）。
_ENGINE_LABELS = {
    "duckduckgo": "DuckDuckGo",
    "baidu": "百度",
    "bing": "Bing",
    "arxiv": "arXiv",
    "exa": "Exa",
    "tavily": "Tavily",
}
_ENGINE_ORDER = ("duckduckgo", "baidu", "bing", "arxiv", "exa", "tavily")

_TOOL_TITLES = {"search.web": "网络搜索（search.web）", "search.fetch": "网页抓取（search.fetch）"}
_TOOL_NOTES = {
    "search.web": "用已启用引擎联网搜索，返回标题 / 链接 / 摘要列表。",
    "search.fetch": "抓取指定 https 网页正文；目标域名须在出口白名单内（默认拒绝）。",
}


class RetrievalPage(QWidget):
    engine_toggle = Signal(str, bool)  # engine, enabled
    key_set = Signal(str, object)  # engine, value(None=清除)
    test_requested = Signal(str)  # engine
    tool_toggle = Signal(str, bool)  # 工具名, enabled
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._engine_rows: dict[str, dict] = {}
        self._tool_rows: dict[str, dict] = {}

        self._title = QLabel("联网检索")
        self._title.setObjectName("pageTitle")
        self.refresh_metrics()

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setToolTip("重读检索配置（文件即配置：手工编辑 modules.json 后点此生效）")
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)

        head = QHBoxLayout()
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(self._refresh_btn)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)

        self._note = QLabel()
        self._note.setObjectName("mutedNote")
        self._note.setWordWrap(True)
        body.addWidget(self._note)

        tools_card, self._tools_box = card()
        self._tools_box.addWidget(section_label("工具"))
        self._tool_box = QVBoxLayout()
        self._tool_box.setContentsMargins(0, 0, 0, 0)
        self._tool_box.setSpacing(0)
        self._tools_box.addLayout(self._tool_box)
        body.addWidget(tools_card)

        engines_card, engines_box = card()
        engines_box.addWidget(section_label("引擎"))
        engines_note = QLabel(
            "启用引擎 = 自动把其端点域名加入出口白名单（停用即移除，其它启用引擎仍需要者保留）。"
            "key 只存本机加密库，保存后不回显；arXiv 遵守 3 秒礼貌速率。"
        )
        engines_note.setObjectName("mutedNote")
        engines_note.setWordWrap(True)
        engines_box.addWidget(engines_note)
        self._engine_box = QVBoxLayout()
        self._engine_box.setContentsMargins(0, 0, 0, 0)
        self._engine_box.setSpacing(0)
        engines_box.addLayout(self._engine_box)
        body.addWidget(engines_card)

        body.addStretch(1)
        holder = QWidget()
        holder.setLayout(body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(holder)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(scroll, 1)

    # -- 状态回推 -----------------------------------------------------------
    def set_state(self, engines: list, tools: list, default_engines: list, module_state: str = "disabled") -> None:
        """按 `retrieval.state` 更新两组行；复用控件，只有消失的行销毁（rev65 纪律）。"""
        self._loading = True
        try:
            tool_infos = {str(t.get("name")): t for t in (tools or [])}
            engine_infos = {str(e.get("name")): e for e in (engines or [])}
            self._clear_layout_items(self._tool_box)
            for name in list(self._tool_rows):
                self._destroy(self._tool_rows.pop(name)["widget"])
            for name in list(self._engine_rows):
                self._destroy(self._engine_rows.pop(name)["widget"])

            if module_state == "disabled":
                self._note.setText(
                    "联网检索模块未启用：在「设置 → 附加功能」中开启后，可在此逐项开关工具与引擎。"
                )
                return
            self._note.setText(
                "以下每一项均可单独开关。启用引擎 = 自动把其端点域名加入出口白名单；"
                "key 只存本机加密库、保存后不回显。"
            )
            self._tool_box.addWidget(section_label("工具"))
            for name in ("search.web", "search.fetch"):
                info = tool_infos.get(name) or {}
                row = self._tool_rows.get(name)
                if row is None:
                    row = self._build_tool_row(name)
                    self._tool_rows[name] = row
                row["switch"].setChecked(bool(info.get("enabled", False)))
                self._tool_box.addWidget(row["widget"])
                row["widget"].show()

            self._engine_box.addWidget(section_label("引擎"))
            order = [str(e) for e in (default_engines or [])]
            display = [n for n in order if n in engine_infos] + [
                n for n in engine_infos if n not in order
            ]
            for name in display:
                if name == "webfetch":
                    continue  # 抓取语义在工具区；引擎行只列六家搜索
                info = engine_infos.get(name) or {}
                row = self._engine_rows.get(name)
                if row is None:
                    row = self._build_engine_row(name, bool(info.get("needs_key")))
                    self._engine_rows[name] = row
                self._update_engine_row(row, info)
                self._engine_box.addWidget(row["widget"])
                row["widget"].show()
        finally:
            self._loading = False

    @staticmethod
    def _destroy(widget: QWidget) -> None:
        widget.setParent(None)  # 重建型界面纪律（v0.0.6 血泪）：先摘再删
        widget.deleteLater()

    def _clear_layout_items(self, layout) -> None:
        while layout.count():
            layout.takeAt(0)

    # -- 行构建 --------------------------------------------------------------
    def _build_tool_row(self, name: str) -> dict:
        widget = QWidget()
        widget.setObjectName("featureRow")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(10)
        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(2)
        label = QLabel(_TOOL_TITLES.get(name, name))
        label.setObjectName("featureName")
        note = QLabel(_TOOL_NOTES.get(name, ""))
        note.setObjectName("mutedNote")
        note.setWordWrap(True)
        copy.addWidget(label)
        copy.addWidget(note)
        switch = Switch()
        switch.setObjectName("featureSwitch")
        switch.setAccessibleName(f"{_TOOL_TITLES.get(name, name)} 启用")
        switch.toggled.connect(lambda checked, n=name: self._on_tool_toggled(n, checked))
        layout.addLayout(copy, 1)
        layout.addWidget(switch)
        return {"widget": widget, "switch": switch, "name": name}

    def _build_engine_row(self, name: str, needs_key: bool) -> dict:
        widget = QWidget()
        widget.setObjectName("featureRow")
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(10)
        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(2)
        label = QLabel(_ENGINE_LABELS.get(name, name))
        label.setObjectName("featureName")
        copy.addWidget(label)
        endpoints = QLabel()
        endpoints.setObjectName("mutedNote")
        endpoints.setWordWrap(True)
        copy.addWidget(endpoints)

        badge = QLabel()
        badge.setObjectName("featureState")
        test_btn = QPushButton("测试")
        test_btn.setToolTip("手动检测该引擎节点：传输保密 / 白名单 / 可达性 / 鉴权 / 结果形态（R1–R5）")
        test_btn.clicked.connect(lambda _c=False, n=name, b=test_btn: self._on_test_clicked(n, b))

        switch = Switch()
        switch.setObjectName("featureSwitch")
        switch.setAccessibleName(f"{_ENGINE_LABELS.get(name, name)} 启用")
        switch.setToolTip("启用 = 自动把端点域名加入出口白名单；停用即移除（其它启用引擎仍需要者保留）")
        switch.toggled.connect(lambda checked, n=name: self._on_engine_toggled(n, checked))

        top.addLayout(copy, 1)
        top.addWidget(badge)
        top.addWidget(test_btn)
        top.addWidget(switch)
        outer.addLayout(top)

        key_row: tuple | None = None
        if needs_key:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            key_edit = QLineEdit()
            key_edit.setEchoMode(QLineEdit.Password)
            key_edit.setPlaceholderText("API key（保存后不回显）")
            key_edit.setMaximumWidth(280)
            save = QPushButton("保存")
            save.clicked.connect(lambda _c=False, n=name, e=key_edit: self._on_key_save(n, e))
            clear = QPushButton("清除")
            clear.clicked.connect(lambda _c=False, n=name: self.key_set.emit(n, None))
            row.addWidget(key_edit)
            row.addWidget(save)
            row.addWidget(clear)
            row.addStretch(1)
            outer.addLayout(row)
            key_row = (key_edit, save, clear)
        return {"widget": widget, "label": label, "endpoints": endpoints, "badge": badge,
                "switch": switch, "test_btn": test_btn, "key_row": key_row, "name": name}

    def _update_engine_row(self, row: dict, info: dict) -> None:
        row["endpoints"].setText("　".join(str(d) for d in info.get("endpoints", [])))
        key_state = str(info.get("key_state", "missing"))
        row["badge"].setText({"stored": "key 已存", "missing": "无 key", "error": "key 库异常"}.get(key_state, key_state))
        row["badge"].setProperty("featureState", {"stored": "ready", "missing": "disabled", "error": "error"}.get(key_state, key_state))
        theme.restyle(row["badge"])
        row["switch"].setChecked(bool(info.get("enabled", False)))

    # -- 事件处理 ------------------------------------------------------------
    def _on_test_clicked(self, name: str, btn) -> None:
        """点「测试」即刻给反馈：按钮转「测试中…」直到结果事件回来（rev68 体验）。"""
        btn.setEnabled(False)
        btn.setText("测试中…")
        self.test_requested.emit(name)

    def _on_engine_toggled(self, name: str, checked: bool) -> None:
        if self._loading:
            return
        self.engine_toggle.emit(name, bool(checked))

    def _on_tool_toggled(self, name: str, checked: bool) -> None:
        if self._loading:
            return
        self.tool_toggle.emit(name, bool(checked))

    def _on_key_save(self, name: str, edit) -> None:
        value = edit.text().strip()
        if value:
            self.key_set.emit(name, value)
            edit.clear()

    def set_test_result(self, engine: str, findings: list, summary: dict) -> None:
        """单引擎测试结果（只读展示；详情过 redact，此前端只显计数）。"""
        row = self._engine_rows.get(engine)
        if row is None:
            return
        passed = int(summary.get("pass", 0))
        failed = int(summary.get("fail", 0))
        skipped = int(summary.get("skip", 0))
        row["badge"].setText(f"测试 {passed}✓ {failed}✗ {skipped}–")
        # 结果事实入 tooltip（证据已过 redact；只显 R 项与状态，不含敏感值）
        row["badge"].setToolTip(
            "\n".join(
                f"{f.get('id', '?')}: {f.get('status', '?')}"
                + (f"　{f.get('evidence', '')}" if f.get("evidence") else "")
                for f in (findings or [])
            )
        )
        btn = row.get("test_btn")
        if btn is not None:
            btn.setEnabled(True)
            btn.setText("测试")

    # -- 外观 -----------------------------------------------------------------
    def refresh_metrics(self, font_size: str | None = None) -> None:
        self._title.setMinimumHeight(text_fit.line_height(self._title, "title", font_size))
