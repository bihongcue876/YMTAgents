"""MCP 插件页（v0.0.3 rev45 / docs 07 §4.3）。

- 服务器列表：增删、启停、重连、状态徽标、工具清单（降权展示）。
- 启用服务器需二次确认（MCP server = 外部代码，docs 07 §6）。
- `GateDialog`：工具调用确认卡片（展示参数原文，供用户审视后放行/拒绝）。
GUI 仅经信号上抛意图（dict/str），信封构造归 MainWindow（gui 不 import core）。
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.widgets import card, key_badge

_STATE_TEXT = {
    "stopped": "未启动",
    "starting": "启动中",
    "ready": "就绪",
    "error": "错误",
    "stopping": "停止中",
}


class ServerDialog(QDialog):
    """服务器编辑对话框；确定后返回配置 dict（不含任何明文密钥）。"""

    def __init__(self, server: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._original_id = (server or {}).get("id") or ""
        self.setWindowTitle("编辑 MCP 服务器" if server else "添加 MCP 服务器")
        self.setMinimumWidth(520)

        self._id = QLineEdit(self._original_id)
        self._id.setPlaceholderText("唯一标识，如 filesystem")
        self._name = QLineEdit((server or {}).get("name", ""))
        self._transport = QComboBox()
        self._transport.addItems(["stdio", "sse", "http"])
        self._transport.setCurrentText((server or {}).get("transport", "stdio"))
        self._command = QLineEdit((server or {}).get("command") or "")
        self._command.setPlaceholderText("如 python 或可执行文件绝对路径（stdio）")
        self._args = QPlainTextEdit("\n".join((server or {}).get("args") or []))
        self._args.setPlaceholderText("每行一个参数")
        self._args.setFixedHeight(64)
        self._url = QLineEdit((server or {}).get("url") or "")
        self._url.setPlaceholderText("https://…（http/sse）")
        self._env = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in ((server or {}).get("env") or {}).items()))
        self._env.setPlaceholderText("每行 KEY=VALUE")
        self._env.setFixedHeight(56)
        self._headers = QPlainTextEdit(
            "\n".join(f"{k}={v}" for k, v in ((server or {}).get("headers_ref") or {}).items())
        )
        self._headers.setPlaceholderText("每行 Name=vault://<id>（凭据引用，值存本地加密库）")
        self._headers.setFixedHeight(56)
        self._enabled = QCheckBox("启用（外部代码，请确认可信）")
        self._enabled.setChecked(bool((server or {}).get("enabled")))

        form = QFormLayout()
        form.addRow("标识", self._id)
        form.addRow("名称", self._name)
        form.addRow("传输", self._transport)
        form.addRow("命令", self._command)
        form.addRow("参数", self._args)
        form.addRow("URL", self._url)
        form.addRow("环境变量", self._env)
        form.addRow("请求头引用", self._headers)
        form.addRow("", self._enabled)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    @staticmethod
    def _pairs(text: str) -> dict:
        out: dict = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
        return out

    def config(self) -> dict:
        return {
            "id": self._id.text().strip(),
            "name": self._name.text().strip() or self._id.text().strip(),
            "transport": self._transport.currentText(),
            "command": self._command.text().strip() or None,
            "args": [ln for ln in self._args.toPlainText().splitlines() if ln.strip()],
            "url": self._url.text().strip() or None,
            "env": self._pairs(self._env.toPlainText()),
            "headers_ref": self._pairs(self._headers.toPlainText()),
            "enabled": self._enabled.isChecked(),
        }


class GateDialog(QDialog):
    """工具调用确认卡片：展示工具名、权限档与参数原文（docs 09 B4）。"""

    def __init__(self, request, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("工具调用确认")
        self.setMinimumWidth(460)
        head = QLabel(f"模型请求调用工具：<b>{request.name}</b><br>权限档：{request.permission}")
        head.setTextFormat(Qt.RichText)
        head.setWordWrap(True)
        try:
            args_text = json.dumps(request.args, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            args_text = str(request.args)
        args = QPlainTextEdit(args_text)
        args.setReadOnly(True)
        args.setMinimumHeight(160)

        allow = QPushButton("允许本次")
        allow.setObjectName("primaryButton")
        deny = QPushButton("拒绝")
        allow.clicked.connect(lambda: self.done(1))
        deny.clicked.connect(lambda: self.done(0))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(deny)
        row.addWidget(allow)

        layout = QVBoxLayout(self)
        layout.addWidget(head)
        layout.addWidget(QLabel("参数原文："))
        layout.addWidget(args)
        layout.addLayout(row)

    def decision(self) -> bool:
        return self.result() == 1


class PluginsPage(QWidget):
    upsert_requested = Signal(dict)
    delete_requested = Signal(str)
    toggle_requested = Signal(str, bool)
    reconnect_requested = Signal(str)
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._servers: list[dict] = []
        self._tools: list[dict] = []

        title = QLabel("MCP 插件")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "接入外部 MCP 服务器：启用后，其工具按权限档参与模型调用。"
            "MCP 服务器是外部代码，请仅接入可信来源；凭据只以 vault:// 引用存储。"
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("mutedNote")

        self._add = QPushButton("＋ 添加服务器")
        self._add.setObjectName("primaryButton")
        self._add.clicked.connect(self._on_add)
        self._refresh = QPushButton("刷新")
        self._refresh.clicked.connect(self.refresh_requested.emit)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self._add)
        head.addWidget(self._refresh)

        self._body = QVBoxLayout()
        self._body.setAlignment(Qt.AlignTop)
        self._body.setSpacing(8)
        holder = QWidget()
        holder.setLayout(self._body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(holder)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(subtitle)
        layout.addWidget(scroll, 1)

    # -- 数据 --------------------------------------------------------------
    def update_servers(self, servers: list[dict]) -> None:
        self._servers = list(servers)
        self._rebuild()

    def update_tools(self, tools: list[dict]) -> None:
        self._tools = list(tools)
        self._rebuild()

    def update_status(self, server_id: str, state: str, tools: list, error) -> None:
        for server in self._servers:
            if server.get("id") == server_id:
                server["state"] = state
                server["tools"] = list(tools or [])
                server["error"] = error
        self._rebuild()

    def _tools_for(self, server_id: str) -> list[dict]:
        return [t for t in self._tools if t.get("server") == server_id]

    def _clear(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild(self) -> None:
        self._clear()
        if not self._servers:
            hint = QLabel("尚未配置 MCP 服务器。点击「＋ 添加服务器」接入。")
            hint.setWordWrap(True)
            self._body.addWidget(hint)
        for server in self._servers:
            self._body.addWidget(self._card(server))
        self._body.addStretch(1)

    def _card(self, server: dict) -> QWidget:
        card_frame, layout = card()
        name = QLabel(f"<b>{server.get('name', '')}</b>　·　{server.get('transport', '')}")
        name.setTextFormat(Qt.RichText)
        state = server.get("state", "stopped")
        # 徽标复用 keyBadge 三态：就绪=绿、错误=红、其余中性（rev57；
        # 原 stateBadge 在 QSS 中无样式，纯文本无状态感）
        badge = key_badge(_STATE_TEXT.get(state, state), {"ready": True, "error": False}.get(state))

        edit = QPushButton("编辑")
        edit.clicked.connect(lambda: self._on_edit(server))
        remove = QPushButton("删除")
        remove.clicked.connect(lambda: self._on_delete(server))
        reconnect = QPushButton("重连")
        reconnect.clicked.connect(lambda: self.reconnect_requested.emit(server.get("id", "")))
        enabled = QCheckBox("启用")
        enabled.setChecked(bool(server.get("enabled")))
        sid = server.get("id", "")
        enabled.toggled.connect(lambda checked, sid=sid: self._on_toggle(sid, checked))

        head = QHBoxLayout()
        head.addWidget(name)
        head.addWidget(badge)
        head.addStretch(1)
        head.addWidget(enabled)
        head.addWidget(reconnect)
        head.addWidget(edit)
        head.addWidget(remove)

        layout.addLayout(head)
        error = server.get("error")
        if error:
            err = QLabel(f"错误：{error}")
            err.setWordWrap(True)
            err.setObjectName("mutedNote")
            layout.addWidget(err)
        tools = self._tools_for(server.get("id", ""))
        if tools:
            tool_text = "　".join(f"{t.get('original', t.get('name'))}（{t.get('permission')}）" for t in tools)
            tool_label = QLabel(f"工具：{tool_text}")
            tool_label.setWordWrap(True)
            tool_label.setObjectName("mutedNote")
            layout.addWidget(tool_label)
        elif state == "ready":
            dim = QLabel("工具：无")
            dim.setObjectName("mutedNote")
            layout.addWidget(dim)
        return card_frame

    # -- 交互 --------------------------------------------------------------
    def _prompt_server(self, server: dict | None) -> None:
        """新增/编辑共用的对话框流程；校验失败或取消则不发请求。"""
        dialog = ServerDialog(server, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        config = dialog.config()
        if not config["id"]:
            QMessageBox.warning(self, "缺少标识", "请填写服务器标识（唯一）。")
            return
        self.upsert_requested.emit(config)

    def _on_add(self) -> None:
        self._prompt_server(None)

    def _on_edit(self, server: dict) -> None:
        self._prompt_server(server)

    def _on_delete(self, server: dict) -> None:
        if QMessageBox.question(self, "删除服务器", f"确定删除「{server.get('name', '')}」？") == QMessageBox.Yes:
            self.delete_requested.emit(server.get("id", ""))

    def _on_toggle(self, server_id: str, enabled: bool) -> None:
        if enabled:
            confirmed = QMessageBox.warning(
                self,
                "启用 MCP 服务器",
                "MCP 服务器是外部代码，可能访问网络或本机资源。\n仅在你信任其来源时启用。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmed != QMessageBox.Yes:
                # 回滚勾选（避免界面与真实状态不一致）
                self._rebuild()
                return
        self.toggle_requested.emit(server_id, enabled)