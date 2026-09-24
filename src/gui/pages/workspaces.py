"""工作区页（v0.0.6）：目录 · 记忆落点 · 文件。

信息结构（spec v0.0.6 §3.11.1）：
- 顶栏：＋新建工作区 / 刷新（重读登记表 —— 文件即配置）；
- 卡片列表：名称 · 目录 · 记忆与文档落点 · 会话数 · 迁移性/存在性/当前 徽标 · 构建命令；
- 选中区：该工作区的**有界**文件列表 + 「打开所在目录」；
- 页脚常驻声明：工作区边界**不是**安全边界（shell 无沙箱，可访问任意路径、可出网）。

取色与取字号一律经 `gui/theme.py`（QSS 属性选择器 `wsBadge[wsState=…]`），本页不写死。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.widgets import card, section_label

#: 数据落点档（值 → 界面文案）。`inline` 是用户裁决的默认档（D3）。
DATA_HOME_CHOICES = (
    ("inline", "工作区内 .ymtdata（默认）"),
    ("managed", "应用数据根内"),
    ("custom", "自定义目录…"),
)

NOTICE = (
    "工作区边界是组织边界，不是安全边界：命令行可访问任意路径、可出网"
    "（不受出口白名单约束）。移除工作区只摘登记，不会删除磁盘上的任何文件。"
)


def _badge(text: str, state: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("wsBadge")
    if state:
        label.setProperty("wsState", state)
    return label


class WorkspaceDialog(QDialog):
    """新建 / 编辑工作区。

    外部目录与「非数据根内」落点**必须勾选知情确认**（spec §3.13 R1/R3）——
    这两项都会把应用的行为面延伸到数据根之外，不能静默通过。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        info: dict | None = None,
        default_managed_kind: str = "inline",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑工作区" if info else "新建工作区")
        self._editing = bool(info)

        self._name = QLineEdit((info or {}).get("name", ""))
        self._name.setPlaceholderText("例如：前端重构、论文实验")

        self._managed = QRadioButton("应用托管（在数据根内自动建目录）")
        self._external = QRadioButton("选择已有文件夹")
        self._managed.setChecked(not (info and info.get("root_kind") == "external"))
        self._external.setChecked(bool(info and info.get("root_kind") == "external"))
        if self._editing:
            self._managed.setEnabled(False)
            self._external.setEnabled(False)
        self._dir = QLineEdit((info or {}).get("root", "") if info else "")
        self._dir.setPlaceholderText("选择或粘贴一个已存在的文件夹")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._pick_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self._dir, 1)
        dir_row.addWidget(browse)

        self._home_kind = QComboBox()
        for value, text in DATA_HOME_CHOICES:
            self._home_kind.addItem(text, value)
        current_kind = (info or {}).get("data_home_kind") or default_managed_kind
        index = self._home_kind.findData(current_kind)
        self._home_kind.setCurrentIndex(index if index >= 0 else 0)
        if self._editing:
            self._home_kind.setEnabled(False)

        self._note = QLineEdit((info or {}).get("note") or "")
        self._note.setPlaceholderText("可选：这个工作区做什么用")
        self._build = QLineEdit((info or {}).get("build_cmd") or "")
        self._build.setPlaceholderText("可选：构建命令（本期仅保存与展示，尚未执行）")

        self._aware = QCheckBox(
            "我了解：该目录内的文件可被本应用与模型通过命令行读写；工作区边界不是安全边界"
        )
        self._aware.setChecked(self._editing)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("名称"))
        layout.addWidget(self._name)
        layout.addWidget(QLabel("位置"))
        layout.addWidget(self._managed)
        layout.addWidget(self._external)
        layout.addLayout(dir_row)
        layout.addWidget(QLabel("记忆与文档落点（本工作区私有的 .ymtdata）"))
        layout.addWidget(self._home_kind)
        layout.addWidget(QLabel("备注"))
        layout.addWidget(self._note)
        layout.addWidget(QLabel("构建命令"))
        layout.addWidget(self._build)
        layout.addWidget(self._aware)
        layout.addWidget(buttons)

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择工作区目录", self._dir.text())
        if path:
            self._dir.setText(path)
            self._external.setChecked(True)

    def _on_accept(self) -> None:
        if not self._name.text().strip():
            QMessageBox.warning(self, "名称不能为空", "请给工作区起一个名字。")
            return
        if self._external.isChecked() and not self._dir.text().strip():
            QMessageBox.warning(self, "未选择目录", "请选择一个已存在的文件夹。")
            return
        external = self._external.isChecked()
        kind = self._home_kind.currentData()
        # 会把行为面延伸到数据根之外的两项：必须显式知情（R1 / §3.11.3）
        if (external or kind != "managed") and not self._aware.isChecked():
            QMessageBox.warning(
                self, "需要确认", "请先勾选下方的知情确认，再创建工作区。"
            )
            return
        self.accept()

    def payload(self) -> dict:
        return {
            "name": self._name.text(),
            "root_kind": "external" if self._external.isChecked() else "managed",
            "root": self._dir.text().strip() or None,
            "data_home_kind": self._home_kind.currentData(),
            "note": self._note.text().strip() or None,
            "build_cmd": self._build.text().strip(),
        }


class WorkspaceMemoryDialog(QDialog):
    """用户显式写入工作区级 `AGENTS.md`；无模型自主写入入口。"""

    def __init__(self, parent: QWidget, workspaces: list[dict], current: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("写入工作区记忆")
        self._scope = QComboBox()
        self._scope.addItem("当前工作区", "current")
        self._scope.addItem("默认工作区", "default")
        self._scope.addItem("指定工作区", "specific")
        self._workspace = QComboBox()
        for info in workspaces:
            self._workspace.addItem(str(info.get("name") or info["id"]), info["id"])
        current_index = self._workspace.findData(current)
        if current_index >= 0:
            self._workspace.setCurrentIndex(current_index)
        self._workspace.setEnabled(False)
        self._mode = QComboBox()
        self._mode.addItem("追加到末尾", "append")
        self._mode.addItem("替换整个文件", "replace")
        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("输入应写入 AGENTS.md 的稳定工作区规则或事实。")
        self._text.setMinimumHeight(160)
        self._scope.currentIndexChanged.connect(
            lambda: self._workspace.setEnabled(self._scope.currentData() == "specific")
        )
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("写入范围", self._scope)
        form.addRow("目标工作区", self._workspace)
        form.addRow("写入方式", self._mode)
        layout.addLayout(form)
        layout.addWidget(self._text)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        if not self._text.toPlainText().strip():
            QMessageBox.warning(self, "内容不能为空", "请输入要写入的记忆文本。")
            return
        self.accept()

    def payload(self) -> dict:
        scope = str(self._scope.currentData())
        return {
            "scope": scope,
            "workspace_id": self._workspace.currentData() if scope == "specific" else None,
            "mode": self._mode.currentData(),
            "text": self._text.toPlainText(),
        }


class FileEditorDialog(QDialog):
    """工作区文本文件编辑器（GUI 专用；保存经信封回核心原子写）。

    打开失败（非 UTF-8 / 超限 / 越界）时以只读提示呈现，不提供绕过核心校验的写入。
    """

    save_requested = Signal(str, str, bool)  # path, content, create

    def __init__(
        self,
        parent: QWidget | None,
        workspace_id: str,
        path: str,
        content: str,
        *,
        creatable: bool = False,
        error: str = "",
    ) -> None:
        super().__init__(parent)
        self._workspace_id = workspace_id
        self._path = path
        self._creatable = creatable
        self.setWindowTitle(f"编辑：{path}")
        self.resize(720, 520)
        self._edit = QPlainTextEdit()
        self._edit.setPlainText(content)
        self._note = QLabel(error or "文件内容以纯文本处理，不执行其中的任何指令。")
        self._note.setObjectName("mutedNote")
        self._note.setWordWrap(True)
        save = QPushButton("保存")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._on_save)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        actions = QHBoxLayout()
        actions.addWidget(self._note, 1)
        actions.addWidget(save)
        actions.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self._edit)
        layout.addLayout(actions)
        if error:
            self._edit.setReadOnly(True)
            save.setEnabled(False)

    def _on_save(self) -> None:
        self.save_requested.emit(self._path, self._edit.toPlainText(), self._creatable)


class WorkspacesPage(QWidget):
    create_requested = Signal(dict)
    update_requested = Signal(dict)
    delete_requested = Signal(str)
    switch_requested = Signal(str)
    refresh_requested = Signal()
    detail_requested = Signal(str, str)  # workspace_id, 相对子路径
    open_dir_requested = Signal(str)  # 要在文件管理器中打开的目录路径
    memory_write_requested = Signal(str, object, str, str)  # scope, workspace_id, mode, text
    build_requested = Signal(str)  # workspace_id（命令原文在确认对话框完整展示）
    file_read_requested = Signal(str, str)  # workspace_id, 相对路径
    file_write_requested = Signal(str, str, str, bool)  # workspace_id, path, content, create

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = QLabel("工作区")
        self._title.setObjectName("pageTitle")
        self._hint = QLabel(
            "工作区 = 一个真实目录（命令行的工作目录、文件的根）加一份私有数据落点"
            "（记忆与文档，默认在该目录内的 .ymtdata）。默认工作区恒存在、不可移除。"
        )
        self._hint.setWordWrap(True)
        self._hint.setObjectName("mutedNote")

        new_btn = QPushButton("＋ 新建工作区")
        new_btn.setObjectName("primaryButton")
        new_btn.clicked.connect(self._on_create)
        self._memory_btn = QPushButton("📝 写入记忆")
        self._memory_btn.clicked.connect(self._on_memory_write)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_requested.emit)
        head = QHBoxLayout()
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(new_btn)
        head.addWidget(self._memory_btn)
        head.addWidget(refresh)

        self._cards = QVBoxLayout()
        self._cards.setAlignment(Qt.AlignTop)
        self._cards.setSpacing(8)
        holder = QWidget()
        holder.setLayout(self._cards)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(holder)

        files_card, files_box = card()
        self._files_title = section_label("文件")
        self._files = QListWidget()
        self._files_note = QLabel("双击文件可在内置编辑器中打开（仅 UTF-8 文本）；打开「文件」即可查看。")
        self._files_note.setWordWrap(True)
        self._files_note.setObjectName("mutedNote")
        self._files.itemDoubleClicked.connect(self._on_file_open)
        files_box.addWidget(self._files_title)
        files_box.addWidget(self._files)
        files_box.addWidget(self._files_note)

        notice = QLabel(NOTICE)
        notice.setWordWrap(True)
        notice.setObjectName("mutedNote")

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(self._hint)
        layout.addWidget(scroll, 1)
        layout.addWidget(files_card)
        layout.addWidget(notice)

        self._workspaces: list[dict] = []
        self._current = ""
        self._default_managed_kind = "inline"
        self._editors: dict[str, FileEditorDialog] = {}
        self._files.setMinimumHeight(60)

    def set_default_managed_kind(self, kind: str) -> None:
        """取 `settings.workspace.default_data_home_kind`（界面只做预填，写入仍由宿主裁决）。"""
        if kind in {value for value, _ in DATA_HOME_CHOICES}:
            self._default_managed_kind = kind

    # -- 数据入口 ----------------------------------------------------------
    def update_workspaces(self, workspaces: list[dict], current: str) -> None:
        self._workspaces = list(workspaces)
        self._current = current or ""
        while self._cards.count():
            item = self._cards.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先摘父再 deleteLater（同侧栏重建）：否则 stale 卡片会被 findChildren 找到
                widget.setParent(None)
                widget.deleteLater()
        if not self._workspaces:
            empty = QLabel("还没有工作区。点「＋ 新建工作区」新建，或点「刷新」重读登记表。")
            empty.setWordWrap(True)
            empty.setObjectName("mutedNote")
            self._cards.addWidget(empty)
            return
        for info in self._workspaces:
            self._cards.addWidget(self._card(info))

    def on_detail(self, result) -> None:
        """`WorkspaceDetailResult` 到达：填充文件列表。"""
        self._files.clear()
        self._files_title.setText(f"文件 · {self._label_of(result.id)}")
        if result.error:
            self._files_note.setText(result.error)
            self._fit_files()
            return
        entries = list(result.entries)
        if not entries:
            self._files_note.setText(f"{result.root} 下没有可显示的条目。")
        else:
            for entry in entries:
                depth = entry.get("path", "").count("/")
                mark = "📁" if entry.get("dir") else "📄"
                item = QListWidgetItem(f"{'    ' * depth}{mark} {entry.get('name', '')}")
                item.setData(Qt.UserRole, f"{entry.get('path', '')}")
                item.setData(Qt.UserRole + 1, bool(entry.get("dir")))
                item.setToolTip(entry.get("path", ""))
                self._files.addItem(item)
            suffix = "（已达上限，仅显示前若干条）" if result.truncated else ""
            self._files_note.setText(f"目录：{result.root}{suffix}")
        self._fit_files()

    def _label_of(self, workspace_id: str) -> str:
        for info in self._workspaces:
            if info.get("id") == workspace_id:
                return str(info.get("name") or workspace_id)
        return workspace_id

    # -- 文本文件编辑 ------------------------------------------------------
    def _on_file_open(self, item: QListWidgetItem) -> None:
        if item.data(Qt.UserRole + 1):  # 目录不打开编辑器
            return
        path = str(item.data(Qt.UserRole) or "")
        if path and self._current:
            self.file_read_requested.emit(self._current, path)

    def on_file_result(self, event) -> None:
        """`workspace.file.result` 到达：读 → 打开编辑器；写 → 提示并刷新列表。"""
        if event.is_write:
            if not event.ok:
                QMessageBox.warning(self, "保存失败", event.error or "保存失败。")
                return
            self._files_note.setText(f"已保存：{event.path}（{event.bytes} 字节）")
            editor = self._editors.pop(event.path, None)
            if editor is not None:
                editor.accept()
            self.detail_requested.emit(event.id, "")
            return
        if not event.ok:
            QMessageBox.warning(self, "无法打开", event.error or "无法打开该文件。")
            return
        dialog = FileEditorDialog(self, event.id, event.path, event.content)
        dialog.save_requested.connect(
            lambda path, content, create, wid=event.id: self.file_write_requested.emit(
                wid, path, content, create
            )
        )
        self._editors[event.path] = dialog
        try:
            dialog.exec()
        finally:
            self._editors.pop(event.path, None)

    def refresh_metrics(self) -> None:
        """字号档位切换后重算文件区高度（样式表字号不参与 sizeHint）。"""
        self._fit_files()

    # -- 卡片 --------------------------------------------------------------
    def _card(self, info: dict) -> QWidget:
        card_frame, layout = card()

        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{info.get('name', info.get('id', ''))}</b>"))
        if info.get("current"):
            header.addWidget(_badge("当前", "current"))
        if info.get("builtin"):
            header.addWidget(_badge("内置"))
        if info.get("missing"):
            header.addWidget(_badge("目录不存在", "missing"))
        if info.get("root_kind") == "external":
            header.addWidget(_badge("外部目录", "external"))
        header.addWidget(
            _badge("可迁移" if info.get("migratable") else "不可迁移")
        )
        if info.get("sessions"):
            header.addWidget(_badge(f"{info['sessions']} 个会话"))
        header.addStretch(1)
        layout.addLayout(header)

        for caption, value in (
            ("目录", info.get("root")),
            ("记忆与文档", info.get("data_home")),
        ):
            row = QLabel(f"{caption}：{value or '—'}")
            row.setWordWrap(True)
            row.setObjectName("mutedNote")
            layout.addWidget(row)

        if info.get("note"):
            note = QLabel(info["note"])
            note.setWordWrap(True)
            note.setObjectName("mutedNote")
            layout.addWidget(note)
        if info.get("build_cmd"):
            build = QLabel(f"构建命令：{info['build_cmd']}")
            build.setWordWrap(True)
            build.setObjectName("mutedNote")
            layout.addWidget(build)
        if info.get("missing"):
            warn = QLabel("该目录已不存在（可能被移动或删除）：仍可更换目录，或移除这条登记。")
            warn.setWordWrap(True)
            warn.setObjectName("mutedNote")
            layout.addWidget(warn)

        actions = QHBoxLayout()
        switch = QPushButton("设为当前")
        switch.setEnabled(not info.get("current"))
        switch.clicked.connect(lambda _=False, wid=info["id"]: self.switch_requested.emit(wid))
        actions.addWidget(switch)
        files = QPushButton("文件")
        files.clicked.connect(lambda _=False, wid=info["id"]: self.detail_requested.emit(wid, ""))
        actions.addWidget(files)
        build = QPushButton("运行构建")
        build.setEnabled(bool(info.get("build_cmd")) and not info.get("missing"))
        build.setToolTip("逐次确认后在当前工作区运行登记的构建命令")
        build.clicked.connect(lambda _=False, item=info: self._on_build(item))
        actions.addWidget(build)
        open_dir = QPushButton("打开目录")
        open_dir.setEnabled(bool(info.get("root")) and not info.get("missing"))
        open_dir.setToolTip("在文件管理器中打开该工作区目录")
        root = info.get("root") or ""
        open_dir.clicked.connect(lambda _=False, path=root: self.open_dir_requested.emit(path))
        actions.addWidget(open_dir)
        edit = QPushButton("编辑")
        edit.clicked.connect(lambda _=False, item=info: self._on_edit(item))
        actions.addWidget(edit)
        remove = QPushButton("移除登记")
        remove.setEnabled(not info.get("builtin"))
        remove.clicked.connect(lambda _=False, item=info: self._on_remove(item))
        actions.addWidget(remove)
        actions.addStretch(1)
        layout.addLayout(actions)
        return card_frame

    # -- 动作 --------------------------------------------------------------
    def _on_create(self) -> None:
        dialog = WorkspaceDialog(self, None, self._default_managed_kind)
        if dialog.exec() != QDialog.Accepted:
            return
        self.create_requested.emit(dialog.payload())

    def _on_memory_write(self) -> None:
        dialog = WorkspaceMemoryDialog(self, self._workspaces, self._current)
        if dialog.exec() == QDialog.Accepted:
            data = dialog.payload()
            self.memory_write_requested.emit(
                data["scope"], data["workspace_id"], data["mode"], data["text"]
            )

    def _on_build(self, info: dict) -> None:
        """运行构建：命令原文完整展示、逐次显式同意（用户即主决策者，不过模型关卡）。

        与 `docs/09` B4 的知情要求一致：命令是用户自己填的、自己点的，故完整展示后执行。
        """
        command = str(info.get("build_cmd") or "").strip()
        if not command:
            return
        answer = QMessageBox.question(
            self,
            "运行构建",
            f"将在「{info.get('name')}」的目录下运行这条命令：\n\n{command}\n\n"
            "命令由你自己填写；运行期间可访问任意路径、可出网（工作区不是安全边界）。",
        )
        if answer == QMessageBox.Yes:
            self.build_requested.emit(str(info["id"]))

    def _on_edit(self, info: dict) -> None:
        dialog = WorkspaceDialog(self, info, self._default_managed_kind)
        if dialog.exec() != QDialog.Accepted:
            return
        payload = dialog.payload()
        payload["id"] = info["id"]
        self.update_requested.emit(payload)

    def _on_remove(self, info: dict) -> None:
        """移除登记的知情确认（R2）—— 必须写明**不删磁盘**。"""
        answer = QMessageBox.question(
            self,
            "移除工作区",
            f"从列表移除「{info.get('name')}」？\n\n"
            "只会移除这条登记，**不会删除磁盘上的任何文件**"
            "（目录与本工作区的 `.ymtdata` 记忆目录均原样保留）。",
        )
        if answer == QMessageBox.Yes:
            self.delete_requested.emit(info["id"])

    def _fit_files(self) -> None:
        """文件区高度按真实行高算（样式表字号不参与 sizeHint —— 项目已知陷阱）。"""
        rows = min(max(self._files.count(), 3), 12)
        row_h = max(self._files.sizeHintForRow(0), self._files.fontMetrics().height() + 6)
        self._files.setMinimumHeight(rows * row_h + 6)
