"""GUI 通用小部件工厂（rev55）。

只放「跨页复用、无业务语义」的小部件构造；样式一律经 objectName 由
`gui.theme.stylesheet` 提供，此处不出现颜色与字号。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QFrame, QLabel, QMenu, QVBoxLayout

from shared.ids import WS_DEFAULT


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


def workspace_new_menu(
    menu: QMenu,
    workspaces: list[dict],
    current_ws: str,
    *,
    on_default,
    on_in: Callable[[str], None],
) -> None:
    """填充「新建会话」的工作区 ▾ 菜单（rev58：侧栏与聊天头同款）。

    `menu` 由调用方持有（usually 按钮的 setMenu），展开时 `/aboutToShow` 调用本函数
    动态填充：首项「当前工作区（…）」走 `on_default`，随后每个工作区一项
    「在「X」新建」走 `on_in(workspace_id)`。菜单项在 QMenu 关闭时自动回收。
    """
    menu.clear()
    current_name = "默认工作区" if current_ws == WS_DEFAULT else current_ws
    for info in workspaces:
        if (info.get("id") or WS_DEFAULT) == current_ws:
            current_name = info.get("name") or current_ws
            break
    first = menu.addAction(f"当前工作区（{current_name}）")
    first.triggered.connect(lambda _=False: on_default())
    menu.addSeparator()
    for info in workspaces:
        wid = info.get("id") or WS_DEFAULT
        name = info.get("name") or ("默认工作区" if wid == WS_DEFAULT else wid)
        action = menu.addAction(f"在「{name}」新建")
        action.triggered.connect(lambda _=False, w=wid: on_in(w))
