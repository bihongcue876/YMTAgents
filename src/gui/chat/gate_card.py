"""关卡卡片（v0.0.11 切片 B）：在对话框内确认受控操作，写族带 old -> new 对照。

设计（spec-2026-09-24-file-tools §6 / §11 D5）：
- 不做模态弹窗、不跳页：卡片挂在消息流下、输入区上；
- 写族按「对照模式」呈现：逐行 - / + 对照（超长折叠，可展开全文）；
- 裁决后**塌缩**为一条记录（如「已修改 a.txt（+2 / -1）」），可再展开；
- 与 gate.result / tool.result 一一对应，回放时同样可见。
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.widgets import card, key_badge, section_label

#: 对照区默认最多显示的行数（超出折叠；折叠只是排版，内容仍在）。
PREVIEW_LINES = 60


def diff_text(preview: dict) -> str:
    """把预览折成 - / + 对照文本；无法对照时返回空串（调用方回退到参数原文）。"""
    old = str(preview.get("old") or "")
    new = str(preview.get("new") or "")
    if not old and not new:
        return ""
    lines: list[str] = []
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    if not old_lines and str(preview.get("kind") or "") == "write":
        lines.append("（新建文件，无旧文）")
    for line in old_lines[:PREVIEW_LINES]:
        lines.append(f"- {line}")
    if len(old_lines) > PREVIEW_LINES:
        lines.append(f"- …（旧文共 {len(old_lines)} 行，已折叠）")
    for line in new_lines[:PREVIEW_LINES]:
        lines.append(f"+ {line}")
    if len(new_lines) > PREVIEW_LINES:
        lines.append(f"+ …（新文共 {len(new_lines)} 行，已折叠）")
    return "\n".join(lines)


class GateCard(QWidget):
    """一次工具调用的确认卡片（非模态；裁决后塌缩为记录，可再展开对照）。"""

    decided = Signal(str, bool)  # call_id, allow

    def __init__(self, event, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.call_id = str(getattr(event, "call_id", "") or "")
        self.name = str(getattr(event, "name", "") or "")
        self._preview = dict(getattr(event, "preview", {}) or {})
        self._resolved = False
        self.setObjectName("gateCard")

        title = QLabel(f"模型请求调用 {self.name}　权限档：{getattr(event, 'permission', 'confirm')}")
        title.setTextFormat(Qt.PlainText)
        title.setWordWrap(True)

        self.detail = QPlainTextEdit()
        self.detail.setObjectName("gateDetail")
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(120)
        self.detail.setMaximumHeight(240)
        self.detail.setPlainText(self._body(event))

        self.record = QLabel("")
        self.record.setObjectName("mutedNote")
        self.record.setWordWrap(True)

        self.toggle = QPushButton("展开对照")
        self.toggle.clicked.connect(self._toggle_detail)

        self.deny = QPushButton("拒绝")
        self.allow = QPushButton("允许本次")
        self.allow.setObjectName("primaryButton")
        self.deny.clicked.connect(lambda: self._decide(False))
        self.allow.clicked.connect(lambda: self._decide(True))

        self.actions = QWidget()
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addStretch(1)
        actions.addWidget(self.deny)
        actions.addWidget(self.allow)

        self.record_row = QWidget()
        record_row = QHBoxLayout(self.record_row)
        record_row.setContentsMargins(0, 0, 0, 0)
        record_row.addWidget(self.record, 1)
        record_row.addWidget(self.toggle)
        self.record_row.setVisible(False)

        # 外观对齐全站卡片（gui.widgets.card）：随主题换肤，不单造样式。
        frame, layout = card()
        head = QHBoxLayout()
        head.addWidget(section_label("关卡确认"))
        head.addStretch(1)
        head.addWidget(key_badge(str(getattr(event, "permission", "confirm")), None))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        body = QVBoxLayout()
        body.setContentsMargins(10, 8, 10, 8)
        body.addWidget(title)
        body.addWidget(self.detail)
        body.addWidget(self.actions)
        body.addWidget(self.record_row)
        frame.setLayout(body)
        layout = outer

    def _body(self, event) -> str:
        text = diff_text(self._preview)
        if text:
            return text
        try:
            return json.dumps(getattr(event, "args", {}) or {}, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(getattr(event, "args", ""))

    def resolved(self) -> bool:
        return self._resolved

    def _decide(self, allow: bool) -> None:
        if self._resolved:
            return
        self._resolved = True
        self._collapse("等待执行…" if allow else "已拒绝（用户）")
        self.toggle.setVisible(False)
        self.decided.emit(self.call_id, allow)

    def resolve(self, summary: str, *, ok: bool | None = None) -> None:
        """塌缩为结果记录（由 gate.result / tool.result 驱动）。"""
        text = summary or ("已完成" if ok else "未完成")
        self._collapse(text)
        self.toggle.setVisible(bool(self.detail.toPlainText().strip()))
        self.toggle.setText("展开对照")

    def _collapse(self, text: str) -> None:
        self.record.setText(text)
        self.actions.setVisible(False)
        self.detail.setVisible(False)
        self.record_row.setVisible(True)

    def _toggle_detail(self) -> None:
        # 注意：父链未显示时 isVisible() 恒为 False —— 判「当前是否被隐藏」要用 isHidden()
        show = self.detail.isHidden()
        self.detail.setVisible(show)
        self.toggle.setText("收起对照" if show else "展开对照")


class GateStack(QWidget):
    """待裁决卡片容器（同一回合可能并发多条）；无卡片时整体隐藏。"""

    decided = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: dict[str, GateCard] = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self.setVisible(False)

    def show_gate(self, event) -> GateCard:
        card = GateCard(event)
        card.decided.connect(self._on_decided)
        self._cards[card.call_id] = card
        self._layout.addWidget(card)
        self.setVisible(True)
        return card

    def card(self, call_id: str) -> GateCard | None:
        return self._cards.get(call_id)

    def count(self) -> int:
        return len(self._cards)

    def resolve(self, call_id: str, summary: str, *, ok: bool | None = None) -> bool:
        card = self._cards.get(call_id)
        if card is None:
            return False
        card.resolve(summary, ok=ok)
        return True

    def clear(self) -> None:
        for card in list(self._cards.values()):
            card.setParent(None)   # 重建型界面：先断父子再 deleteLater（AGENTS §6）
            card.deleteLater()
        self._cards.clear()
        self.setVisible(False)

    def _on_decided(self, call_id: str, allow: bool) -> None:
        if not allow:
            self.resolve(call_id, "已拒绝（用户）", ok=False)
        self.decided.emit(call_id, allow)
