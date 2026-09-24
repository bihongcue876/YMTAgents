"""小图书馆页：库管理、外部对话、事件、只读检索与原生图谱。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui import theme
from gui.widgets import card


class LibraryDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        info: dict | None = None,
        model_refs: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._info = dict(info or {})
        self.setWindowTitle("编辑书库" if info else "新建书库")
        self._name = QLineEdit(str(self._info.get("name") or ""))
        self._kind = QComboBox()
        self._kind.addItem("应用托管", "managed")
        self._kind.addItem("外部目录", "external")
        kind = str(self._info.get("root_kind") or "managed")
        self._kind.setCurrentIndex(max(0, self._kind.findData(kind)))
        self._kind.setEnabled(not bool(info))
        self._root = QLineEdit(str(self._info.get("root") or self._info.get("resolved_root") or ""))
        self._browse = QPushButton("浏览…")
        self._browse.clicked.connect(self._pick_root)
        root_row = QHBoxLayout()
        root_row.addWidget(self._root, 1)
        root_row.addWidget(self._browse)
        self._group = QLineEdit(str(self._info.get("group") or ""))
        self._model = QComboBox()
        self._model.setEditable(True)
        refs = list(dict.fromkeys([*(model_refs or []), "main", "thinking", "fast", "embedding"]))
        for ref in refs:
            self._model.addItem(ref, ref)
        current_ref = str(self._info.get("model_ref") or "main")
        index = self._model.findData(current_ref)
        if index >= 0:
            self._model.setCurrentIndex(index)
        else:
            self._model.setCurrentText(current_ref)
        self._note = QLineEdit(str(self._info.get("note") or ""))
        self._aware = QCheckBox("我确认该外部目录将由本应用作为书库文件目录使用")
        self._aware.setChecked(bool(info))
        self._aware.setVisible(kind == "external")
        self._kind.currentIndexChanged.connect(self._on_kind_changed)
        self._on_kind_changed(self._kind.currentIndex())

        form = QFormLayout()
        form.addRow("书库名称", self._name)
        form.addRow("存储方式", self._kind)
        form.addRow("外部目录", root_row)
        form.addRow("分组（可选）", self._group)
        form.addRow("模型引用（槽位或模型 ID）", self._model)
        form.addRow("备注", self._note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._aware)
        layout.addWidget(buttons)

    def _on_kind_changed(self, _index: int) -> None:
        external = self._kind.currentData() == "external"
        self._root.setEnabled(external)
        self._browse.setEnabled(external)
        self._aware.setVisible(external)

    def _pick_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择书库目录", self._root.text())
        if path:
            self._root.setText(path)

    def _validate(self) -> None:
        if not self._name.text().strip():
            QMessageBox.warning(self, "名称不能为空", "请为书库填写名称。")
            return
        if self._kind.currentData() == "external":
            if not self._root.text().strip():
                QMessageBox.warning(self, "未选择目录", "请选择一个已存在的目录。")
                return
            if not self._aware.isChecked():
                QMessageBox.warning(self, "需要确认", "请先确认外部目录的使用范围。")
                return
        if not self._model.currentText().strip():
            QMessageBox.warning(self, "模型引用不能为空", "选择模型槽位或填写一个模型 ID。")
            return
        self.accept()

    def payload(self) -> dict:
        return {
            "name": self._name.text().strip(),
            "root_kind": self._kind.currentData(),
            "root": self._root.text().strip() or None,
            "group": self._group.text().strip(),
            "model_ref": self._model.currentText().strip(),
            "note": self._note.text().strip(),
        }


class LibraryGraphView(QGraphicsView):
    node_selected = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        self._scene = QGraphicsScene()
        super().__init__(self._scene, parent)
        self._theme = theme.DEFAULT_THEME
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setMinimumHeight(230)
        self._scene.selectionChanged.connect(self._on_selection_changed)
        self.setObjectName("libraryGraph")

    def set_theme(self, name: str) -> None:
        self._theme = name
        colors = theme.palette(name)
        self.setBackgroundBrush(QColor(colors.bg))
        self.viewport().update()

    def set_graph(self, nodes: list[dict], edges: list[dict], truncated: bool = False) -> None:
        self._scene.clear()
        palette = theme.palette(self._theme)
        colors = [palette.accent, palette.ok, palette.warn, palette.danger]
        selected_nodes = nodes[:500]
        positions: dict[str, tuple[float, float]] = {}
        for index, node in enumerate(selected_nodes):
            node_id = str(node.get("node_id") or "")
            row, col = divmod(index, 3)
            positions[node_id] = (col * 240.0, row * 130.0)

        for edge in edges[:1500]:
            start = positions.get(str(edge.get("source") or ""))
            end = positions.get(str(edge.get("target") or ""))
            if start is None or end is None:
                continue
            line = QGraphicsLineItem(start[0] + 170, start[1] + 36, end[0] + 10, end[1] + 36)
            line.setPen(QPen(QColor(palette.border)))
            self._scene.addItem(line)
            label = QGraphicsTextItem(str(edge.get("relation") or ""))
            label.setPlainText(str(edge.get("relation") or ""))
            label.setDefaultTextColor(QColor(palette.muted))
            label.setPos((start[0] + end[0]) / 2 + 85, (start[1] + end[1]) / 2 + 30)
            self._scene.addItem(label)

        for index, node in enumerate(selected_nodes):
            node_id = str(node.get("node_id") or "")
            x, y = positions[node_id]
            library_id = str(node.get("library_id") or "")
            color = colors[sum(library_id.encode("utf-8")) % len(colors)] if library_id else colors[0]
            item = QGraphicsEllipseItem(x, y, 180, 74)
            item.setBrush(QColor(palette.surface))
            item.setPen(QPen(QColor(color), 2))
            item.setFlag(QGraphicsEllipseItem.ItemIsSelectable, True)
            item.setData(Qt.UserRole, dict(node))
            self._scene.addItem(item)
            text = QGraphicsTextItem(str(node.get("title") or ""), item)
            text.setPlainText(str(node.get("title") or ""))
            text.setTextWidth(155)
            text.setDefaultTextColor(QColor(palette.fg))
            text.setPos(x + 12, y + 12)
        self._scene.setSceneRect(self._scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))
        if self._scene.items():
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self._truncated = bool(truncated or len(nodes) > 500 or len(edges) > 1500)

    def _on_selection_changed(self) -> None:
        selected = [item for item in self._scene.selectedItems() if item.data(Qt.UserRole)]
        if selected:
            self.node_selected.emit(selected[0].data(Qt.UserRole))


class LibraryPage(QWidget):
    create_requested = Signal(dict)
    update_requested = Signal(dict)
    delete_requested = Signal(str)
    switch_requested = Signal(str)
    refresh_requested = Signal(object)
    detail_requested = Signal(str, int, int, int, object, object)
    ingest_requested = Signal(str, str, str, bool, object)
    query_requested = Signal(object, str, str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._available = False
        self._libraries: list[dict] = []
        self._current: str | None = None
        self._model_refs: list[str] = []
        self._selected_event: str | None = None
        self._last_query_results: list[dict] = []
        self._selected_source_refs: list[str] = []
        self._selected_source_library: str | None = None
        self._event_offset = 0
        self._event_limit = 50
        self._loading_libraries = False
        self._theme = theme.DEFAULT_THEME

        self._title = QLabel("小图书馆")
        self._title.setObjectName("pageTitle")
        self._hint = QLabel("管理本地书库、记录外部对话，并按需只读检索与查看来源图谱。")
        self._hint.setObjectName("mutedNote")
        self._hint.setWordWrap(True)
        self._create = QPushButton("＋ 新建书库")
        self._create.setObjectName("primaryButton")
        self._edit = QPushButton("编辑")
        self._delete = QPushButton("移除登记")
        self._refresh = QPushButton("刷新")
        header = QHBoxLayout()
        header.addWidget(self._title)
        header.addStretch(1)
        header.addWidget(self._edit)
        header.addWidget(self._delete)
        header.addWidget(self._create)
        header.addWidget(self._refresh)

        self._library_list = QListWidget()
        self._library_list.setMinimumWidth(210)
        self._library_list.setMaximumWidth(330)
        self._library_list.itemClicked.connect(self._on_library_clicked)
        self._library_list.itemChanged.connect(self._on_filter_changed)
        self._library_list.currentItemChanged.connect(self._update_actions)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("书库（勾选项用于联合检索/图谱）"))
        left_layout.addWidget(self._library_list, 1)
        self._status = QLabel("DPIM 尚未启用。请在「设置 → 附加功能」中启用小图书馆。")
        self._status.setObjectName("mutedNote")
        self._status.setWordWrap(True)
        left_layout.addWidget(self._status)

        self._tabs = QTabWidget()
        self._events_tab = self._build_events_tab()
        self._query_tab = self._build_query_tab()
        self._graph_tab = self._build_graph_tab()
        self._tabs.addTab(self._events_tab, "外部对话与事件")
        self._tabs.addTab(self._query_tab, "检索调试台")
        self._tabs.addTab(self._graph_tab, "图谱")
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self._tabs)
        splitter.setStretchFactor(1, 1)

        self._create.clicked.connect(self._on_create)
        self._edit.clicked.connect(self._on_edit)
        self._delete.clicked.connect(self._on_delete)
        self._refresh.clicked.connect(self._on_refresh)
        self._update_actions()
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._hint)
        layout.addWidget(splitter, 1)

    def _build_events_tab(self) -> QWidget:
        page = QWidget()
        event_list = QListWidget()
        event_list.itemClicked.connect(self._on_event_clicked)
        self._event_list = event_list
        self._event_content = QPlainTextEdit()
        self._event_content.setReadOnly(True)
        self._event_content.setPlaceholderText("选择一个事件查看原文。")
        self._retry = QPushButton("重试索引")
        self._skip = QPushButton("跳过索引")
        self._events_prev = QPushButton("上一页")
        self._events_next = QPushButton("下一页")
        self._events_prev.setEnabled(False)
        self._events_next.setEnabled(False)
        self._events_prev.clicked.connect(lambda: self._change_event_page(-self._event_limit))
        self._events_next.clicked.connect(lambda: self._change_event_page(self._event_limit))
        self._retry.setEnabled(False)
        self._skip.setEnabled(False)
        self._retry.clicked.connect(lambda: self._send_existing_event(True))
        self._skip.clicked.connect(lambda: self._send_existing_event(False))
        event_actions = QHBoxLayout()
        event_actions.addWidget(self._retry)
        event_actions.addWidget(self._skip)
        event_actions.addWidget(self._events_prev)
        event_actions.addWidget(self._events_next)
        event_actions.addStretch(1)
        event_box, event_layout = card()
        event_layout.addWidget(QLabel("事件（原文只保存在当前书库）"))
        event_layout.addWidget(event_list, 1)
        detail_box, detail_layout = card()
        detail_layout.addWidget(QLabel("事件详情"))
        detail_layout.addWidget(self._event_content, 1)
        detail_layout.addLayout(event_actions)
        # 垂直分栏让事件页在窄窗口中仍可读；横向双列会把主窗最小宽度推高。
        event_splitter = QSplitter(Qt.Vertical)
        event_splitter.addWidget(event_box)
        event_splitter.addWidget(detail_box)
        event_splitter.setStretchFactor(1, 1)

        self._ingest_type = QComboBox()
        for value, label in (("interaction", "对话"), ("data", "资料"), ("source", "来源")):
            self._ingest_type.addItem(label, value)
        self._ingest = QPlainTextEdit()
        self._ingest.setPlaceholderText("输入外部对话或资料；以 ^ 开头可运行小图书馆指令。")
        self._ingest.setMaximumHeight(100)
        send = QPushButton("保存并索引")
        send.setObjectName("primaryButton")
        send.clicked.connect(self._on_ingest)
        input_row = QHBoxLayout()
        input_row.addWidget(self._ingest_type)
        input_row.addWidget(send)
        page_layout = QVBoxLayout(page)
        page_layout.addWidget(event_splitter, 1)
        page_layout.addWidget(self._ingest)
        page_layout.addLayout(input_row)
        return page

    def _build_query_tab(self) -> QWidget:
        page = QWidget()
        self._query_text = QLineEdit()
        self._query_text.setPlaceholderText("输入关键词或问题（最多 4000 字符）")
        self._query_mode = QComboBox()
        for value, label in (("hybrid", "混合"), ("events", "事件原文"), ("nodes", "知识节点")):
            self._query_mode.addItem(label, value)
        self._query_top_k = QSpinBox()
        self._query_top_k.setRange(1, 50)
        self._query_top_k.setValue(8)
        self._query_top_k.setToolTip("最多显示的检索结果数")
        self._query_run = QPushButton("检索")
        self._query_run.setObjectName("primaryButton")
        self._query_run.clicked.connect(self._on_query)
        query_row = QHBoxLayout()
        query_row.addWidget(self._query_text, 1)
        query_row.addWidget(self._query_mode)
        query_row.addWidget(self._query_top_k)
        query_row.addWidget(self._query_run)
        self._results = QListWidget()
        self._results.itemClicked.connect(self._on_result_clicked)
        self._result_content = QPlainTextEdit()
        self._result_content.setReadOnly(True)
        self._result_source = QPushButton("查看来源事件")
        self._result_source.setEnabled(False)
        self._result_source.clicked.connect(self._open_source_event)
        self._debug = QPlainTextEdit()
        self._debug.setReadOnly(True)
        self._debug.setPlaceholderText("检索调试信息（逐库召回、RRF 排名与来源锚点）")
        lower = QSplitter(Qt.Vertical)
        lower.addWidget(self._result_content)
        lower.addWidget(self._debug)
        lower.setStretchFactor(0, 2)
        lower.setStretchFactor(1, 1)
        row = QSplitter(Qt.Horizontal)
        row.addWidget(self._results)
        row.addWidget(lower)
        row.setStretchFactor(1, 1)
        layout = QVBoxLayout(page)
        layout.addLayout(query_row)
        layout.addWidget(row, 1)
        layout.addWidget(self._result_source)
        return page

    def _build_graph_tab(self) -> QWidget:
        page = QWidget()
        self._graph = LibraryGraphView()
        self._graph_compare = LibraryGraphView()
        self._graph_compare.setVisible(False)
        self._graph_splitter = QSplitter(Qt.Horizontal)
        self._graph_splitter.addWidget(self._graph)
        self._graph_splitter.addWidget(self._graph_compare)
        self._graph_splitter.setStretchFactor(0, 1)
        self._graph_splitter.setStretchFactor(1, 1)
        self._graph_info = QPlainTextEdit()
        self._graph_info.setReadOnly(True)
        self._graph_info.setMaximumHeight(95)
        self._graph_source = QPushButton("跳转到来源事件")
        self._graph_source.setEnabled(False)
        self._graph_source.clicked.connect(self._open_source_event)
        self._graph.node_selected.connect(self._on_node_selected)
        self._graph_compare.node_selected.connect(self._on_node_selected)
        layout = QVBoxLayout(page)
        layout.addWidget(self._graph_splitter)
        layout.addWidget(self._graph_info)
        layout.addWidget(self._graph_source)
        return page

    def set_theme(self, name: str) -> None:
        self._theme = name
        self._graph.set_theme(name)
        self._graph_compare.set_theme(name)

    def set_available(self, available: bool, state: str = "disabled") -> None:
        self._available = bool(available)
        self._create.setEnabled(self._available)
        self._edit.setEnabled(self._available and bool(self._current))
        self._delete.setEnabled(self._available and bool(self._current))
        self._refresh.setEnabled(self._available)
        self._tabs.setEnabled(self._available)
        if not available:
            self._status.setText("小图书馆已关闭；书库数据未读取。请在「设置 → 附加功能」启用 DPIM。")
            self.update_libraries([], None)
            self._event_list.clear()
            self._event_content.clear()
            self._graph.set_graph([], [])
            self._graph_compare.set_graph([], [])
            self._graph_compare.setVisible(False)
            self._results.clear()
            self._result_content.clear()
            self._debug.clear()
        elif state in {"degraded", "error"}:
            self._status.setText("小图书馆处于降级状态；检查书库索引或数据文件后可刷新恢复。")
        else:
            self._status.setText("选择书库以开始管理、记录对话或检索。")

    def set_providers(self, providers: list[Any]) -> None:
        refs = ["main", "thinking", "fast", "embedding"]
        for provider in providers or []:
            refs.extend(model.id for model in getattr(provider, "models", []) if getattr(model, "id", ""))
        self._model_refs = list(dict.fromkeys(refs))

    def set_error(self, message: str) -> None:
        self._status.setText(str(message or "操作失败。"))

    def update_libraries(self, libraries: list[dict], current: str | None) -> None:
        self._loading_libraries = True
        self._libraries = [dict(item) for item in libraries or []]
        self._current = current
        checked = self._checked_ids()
        self._library_list.clear()
        for info in self._libraries:
            group = f" · {info['group']}" if info.get("group") else ""
            item = QListWidgetItem(f"{info.get('name') or info.get('id')}{group}")
            item.setData(Qt.UserRole, info.get("id"))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked if info.get("id") in checked or not checked else Qt.Unchecked)
            if info.get("id") == current:
                item.setText("● " + item.text())
            if info.get("missing"):
                item.setToolTip("外部书库目录不存在；可编辑并重新指向目录。")
            self._library_list.addItem(item)
        current_row = next(
            (i for i, info in enumerate(self._libraries) if info.get("id") == current), -1
        )
        if current_row >= 0:
            self._library_list.setCurrentRow(current_row)
        self._loading_libraries = False
        self._update_actions()

    def on_list(self, event: Any) -> None:
        self.update_libraries(event.libraries, event.current)
        if event.error:
            self._status.setText(str(event.error))

    def on_detail(self, event: Any) -> None:
        if event.error:
            self._status.setText(str(event.error))
        self._event_list.clear()
        self._selected_event = None
        self._event_content.clear()
        self._retry.setEnabled(False)
        self._skip.setEnabled(False)
        for row in event.events:
            status = row.get("status", "raw")
            title = str(row.get("raw_content") or "").replace("\n", " ")[:120]
            item = QListWidgetItem(f"[{status}] {title}")
            item.setData(Qt.UserRole, row)
            item.setToolTip(str(row.get("created_at") or ""))
            self._event_list.addItem(item)
        focus_id = self._selected_source_refs[0] if self._selected_source_refs else None
        if focus_id:
            for index in range(self._event_list.count()):
                item = self._event_list.item(index)
                if (item.data(Qt.UserRole) or {}).get("event_id") == focus_id:
                    self._event_list.setCurrentItem(item)
                    self._on_event_clicked(item)
                    break
        self._event_offset = int(event.event_offset)
        self._event_limit = int(event.event_limit)
        total = int(event.counts.get("total", 0))
        self._events_prev.setEnabled(self._event_offset > 0)
        self._events_next.setEnabled(self._event_offset + len(event.events) < total)
        self._status.setText(
            f"事件 {event.counts.get('total', 0)} · 节点 {event.counts.get('nodes', 0)} · "
            f"关系 {event.counts.get('edges', 0)} · {event.root}"
        )

    def on_graph(self, event: Any) -> None:
        if len(event.library_ids) == 2:
            self._graph_compare.setVisible(True)
            for view, library_id in zip((self._graph, self._graph_compare), event.library_ids):
                nodes = [node for node in event.nodes if node.get("library_id") == library_id]
                node_ids = {node.get("node_id") for node in nodes}
                edges = [
                    edge for edge in event.edges
                    if edge.get("library_id") == library_id
                    and edge.get("source") in node_ids and edge.get("target") in node_ids
                ]
                view.set_graph(nodes, edges, event.truncated)
        else:
            self._graph_compare.setVisible(False)
            self._graph.set_graph(event.nodes, event.edges, event.truncated)
        if event.truncated:
            self._graph_info.setPlainText("图谱已按上限截断；可通过筛选书库缩小范围。选择两个书库可并列对照。")

    def on_ingest_result(self, event: Any) -> None:
        self._status.setText(str(event.message))
        if not event.event_id or event.status in {"indexed", "linked"}:
            self._ingest.clear()

    def on_query_result(self, event: Any) -> None:
        self._last_query_results = list(event.results)
        self._selected_source_refs = []
        self._selected_source_library = None
        self._result_source.setEnabled(False)
        self._results.clear()
        for index, result in enumerate(self._last_query_results):
            sources = ", ".join(
                str(ref.get("library_name") or ref.get("library_id"))
                for ref in result.get("source_libraries", [])
            )
            item = QListWidgetItem(
                f"{result.get('title') or result.get('kind')} · {sources} · {float(result.get('score', 0)):.4f}"
            )
            item.setData(Qt.UserRole, index)
            self._results.addItem(item)
        self._debug.setPlainText(self._plain_json(event.debug))
        self._result_content.setPlainText("" if not self._last_query_results else "选择结果查看来源与内容。")

    def refresh_metrics(self, _font_size: str | None = None) -> None:
        return

    def _checked_ids(self) -> list[str]:
        result = []
        for index in range(self._library_list.count()):
            item = self._library_list.item(index)
            if item.checkState() == Qt.Checked and item.data(Qt.UserRole):
                result.append(str(item.data(Qt.UserRole)))
        return result

    def _selected_graph_ids(self) -> list[str] | None:
        selected = self._checked_ids()
        return (selected[:20] if selected else ([self._current] if self._current else None))

    def _on_create(self) -> None:
        dialog = LibraryDialog(self, model_refs=self._model_refs)
        if dialog.exec() == QDialog.Accepted:
            self.create_requested.emit(dialog.payload())

    def _selected_info(self) -> dict | None:
        item = self._library_list.currentItem()
        library_id = str(item.data(Qt.UserRole) or "") if item else ""
        return next((info for info in self._libraries if info.get("id") == library_id), None)

    def _update_actions(self, *_args) -> None:
        has_item = self._selected_info() is not None
        self._edit.setEnabled(self._available and has_item)
        self._delete.setEnabled(self._available and has_item)

    def _on_edit(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        dialog = LibraryDialog(self, info=info, model_refs=self._model_refs)
        if dialog.exec() != QDialog.Accepted:
            return
        payload = dialog.payload()
        payload["id"] = info["id"]
        # 存储方式由创建时确定；managed root 不作为任意路径写入请求。
        if info.get("root_kind") == "managed":
            payload["root"] = None
        self.update_requested.emit(payload)

    def _on_delete(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        answer = QMessageBox.question(
            self,
            "移除书库登记",
            f"从列表移除「{info.get('name')}」？\n\n只移除登记，不会删除该目录或其中的任何文件。",
        )
        if answer == QMessageBox.Yes:
            self.delete_requested.emit(str(info["id"]))

    def _on_library_clicked(self, item: QListWidgetItem) -> None:
        library_id = str(item.data(Qt.UserRole) or "")
        if library_id:
            self._event_offset = 0
            self._selected_source_refs = []
            self.switch_requested.emit(library_id)

    def _on_filter_changed(self, _item: QListWidgetItem) -> None:
        if not self._loading_libraries and self._current:
            self._request_detail(self._current)

    def _on_refresh(self) -> None:
        self.refresh_requested.emit(self._current)

    def _request_detail(self, library_id: str | None = None) -> None:
        target = library_id or self._current
        if target:
            self.detail_requested.emit(
                target, self._event_offset, self._event_limit, 300,
                self._selected_graph_ids(), None,
            )

    def _change_event_page(self, delta: int) -> None:
        self._event_offset = max(0, self._event_offset + delta)
        self._request_detail()

    def _on_event_clicked(self, item: QListWidgetItem) -> None:
        row = item.data(Qt.UserRole) or {}
        self._selected_event = str(row.get("event_id") or "") or None
        self._event_content.setPlainText(str(row.get("raw_content") or ""))
        retryable = row.get("status") in {"raw", "failed", "skipped"}
        self._retry.setEnabled(retryable and bool(self._selected_event))
        self._skip.setEnabled(row.get("status") in {"raw", "failed"} and bool(self._selected_event))

    def _send_existing_event(self, index: bool) -> None:
        if self._current and self._selected_event:
            self.ingest_requested.emit(self._current, "", "interaction", index, self._selected_event)

    def _on_ingest(self) -> None:
        text = self._ingest.toPlainText().strip()
        if self._current and text:
            self.ingest_requested.emit(
                self._current, text, str(self._ingest_type.currentData()), True, None
            )

    def _on_query(self) -> None:
        query = self._query_text.text().strip()
        if query:
            selected = self._checked_ids() or None
            self.query_requested.emit(
                selected, query, str(self._query_mode.currentData()), self._query_top_k.value()
            )

    def _on_result_clicked(self, item: QListWidgetItem) -> None:
        index = int(item.data(Qt.UserRole))
        if 0 <= index < len(self._last_query_results):
            result = self._last_query_results[index]
            self._result_content.setPlainText(self._plain_json(result))
            self._selected_source_refs = list(result.get("event_refs") or [])
            libraries = result.get("source_libraries") or []
            self._selected_source_library = (
                str(libraries[0].get("library_id")) if libraries else None
            )
            self._result_source.setEnabled(bool(self._selected_source_refs))

    def _on_node_selected(self, node: dict) -> None:
        self._graph_info.setPlainText(self._plain_json(node))
        self._selected_source_refs = list(node.get("source_refs") or [])
        self._selected_source_library = str(node.get("library_id") or self._current or "") or None
        self._graph_source.setEnabled(bool(self._selected_source_refs))

    def _open_source_event(self) -> None:
        if not self._selected_source_refs or not self._selected_source_library:
            return
        event_id = str(self._selected_source_refs[0])
        library_id = self._selected_source_library
        self._current = library_id
        self.switch_requested.emit(library_id)
        self.detail_requested.emit(library_id, 0, self._event_limit, 300, None, event_id)

    @staticmethod
    def _plain_json(value: Any) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2)
