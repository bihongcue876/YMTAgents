"""GUI 通用小部件工厂（rev55）。

只放「跨页复用、无业务语义」的小部件构造；样式一律经 objectName 由
`gui.theme.stylesheet` 提供，此处不出现颜色与字号。
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


def section_label(text: str) -> QLabel:
    """配置页节标题（`sectionLabel`：muted + 600 字重，小节分组感）。"""
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    return label


def card() -> tuple[QFrame, QVBoxLayout]:
    """内容卡片（`card`）：配置页与列表页的模块容器（rev57）。

    底色、边框、圆角由 theme QSS 提供；此处只定内边距与控件间距。
    返回 (框架, 布局) —— 布局已挂到框架上，调用方直接往里放内容。
    """
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(6)
    return frame, layout


def key_badge(text: str, ok: bool | None = None) -> QLabel:
    """状态徽章（`keyBadge`）：ok 三态 —— True/False 着色，None 中性。"""
    badge = QLabel(text)
    badge.setObjectName("keyBadge")
    if ok is not None:
        badge.setProperty("keyStored", ok)
    return badge
