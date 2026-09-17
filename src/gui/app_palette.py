"""把 theme token 灌进 QApplication 调色板（spec rev12 §1）。

**为什么需要**：主题此前只靠 QSS 生效，但 Qt 有一批绘制**不走 QSS**、直接取调色板 ——
QFrame 边框（Mid/Dark）、禁用态文字、微调框箭头、未覆盖控件的本底等。
实测应用暗色主题后 `QApplication.palette()` 仍是亮色默认
（Window 是浅灰、Base 是纯白、Mid/Dark 是中灰），
于是暗色界面上留着亮灰的边框与控件 —— 用户看到的「暗色模式刷新不完整」的主要来源之一。
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

from gui import theme


def build(name: str | None) -> QPalette:
    """按主题 token 构造 QPalette（颜色只来自 theme，不引入第二份来源）。"""
    p = theme.palette(name)
    qp = QPalette()
    qp.setColor(QPalette.Window, QColor(p.bg))
    qp.setColor(QPalette.WindowText, QColor(p.fg))
    qp.setColor(QPalette.Base, QColor(p.surface))
    qp.setColor(QPalette.AlternateBase, QColor(p.bg))
    qp.setColor(QPalette.Text, QColor(p.fg))
    qp.setColor(QPalette.Button, QColor(p.surface))
    qp.setColor(QPalette.ButtonText, QColor(p.fg))
    qp.setColor(QPalette.Highlight, QColor(p.accent))
    qp.setColor(QPalette.HighlightedText, QColor(p.bg))
    qp.setColor(QPalette.Light, QColor(p.bg))
    qp.setColor(QPalette.Mid, QColor(p.border))  # QFrame::StyledPanel 的边框由它绘制
    qp.setColor(QPalette.Dark, QColor(p.border))
    qp.setColor(QPalette.ToolTipBase, QColor(p.surface))
    qp.setColor(QPalette.ToolTipText, QColor(p.fg))
    qp.setColor(QPalette.PlaceholderText, QColor(p.muted))
    # 禁用态：暗色下应「更暗」，默认调色板给的是亮灰，观感像没禁用
    qp.setColor(QPalette.Disabled, QPalette.Text, QColor(p.muted))
    qp.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(p.muted))
    qp.setColor(QPalette.Disabled, QPalette.WindowText, QColor(p.muted))
    return qp


def apply(name: str | None) -> QPalette | None:
    """把调色板应用到当前 QApplication；无 Qt 环境时返回 None。"""
    from PySide6.QtWidgets import QApplication

    qp = build(name)
    app = QApplication.instance()
    if app is not None:
        app.setPalette(qp)
    return qp
