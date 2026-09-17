"""让标签宽高跟上字号档位（docs 05 §1 视觉规范的收尾一步）。

**要解决的问题**：样式表里的 `font-size` 不参与 `sizeHint` 计算 ——
布局只会拿到「按默认字号估出来」的宽**与高**，于是文本被裁。
横向（rev11）：空状态标题需 252px 只得 240px；
纵向（rev16）：换行标签的行高按默认字号估，真实字号大一号时每行都被腰斩
（截图实证：空状态两行文案同时被切半）。

**为什么不写在 `gui.theme`**：`theme` 的契约是「只依赖标准库」（只算 token），
而这里必须用 Qt 的字体度量，故单列一个薄模块。字号仍**只从 `theme` 取**，不引入第二份来源。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics

from gui import theme

#: 文本两侧留白（与 QSS padding 无关，只用于抵消字形的左右边距）
PAD_PX = 8
#: 纵向留白（抵消字形上下边距）
PAD_H = 4


def _token_font(label, token: str, font_size: str | None) -> QFont:
    font = QFont(label.font())
    font.setPixelSize(theme.font_px(token, font_size))
    return font


def fit_label(label, token: str = "ui", font_size: str | None = None) -> int:
    """按该档位的真实像素字号量一遍文本，设为标签最小宽度；返回该宽度。

    调用时机：样式表已应用之后（`theme.apply(...)`），且每次**档位变化**都要重算，
    因为宽度依赖字号。返回宽度便于测试断言。
    """
    width = QFontMetrics(_token_font(label, token, font_size)).horizontalAdvance(label.text()) + PAD_PX
    label.setMinimumWidth(width)
    return width


def fit_wrapped_label(label, text: str, token: str = "ui", font_size: str | None = None) -> int:
    """换行标签的**最小高度**：按当前宽度 + 真实字号量出实际需要的行数与高度。

    在 `resizeEvent` 里调用（宽度变化会改变行数），返回该高度便于测试断言。
    """
    font = _token_font(label, token, font_size)
    fm = QFontMetrics(font)
    width = max(label.width(), 1)
    need = fm.boundingRect(0, 0, width, 1_000_000, Qt.TextWordWrap, text).height()
    label.setMinimumHeight(need + PAD_H)
    return need + PAD_H


def line_height(label, token: str = "ui", font_size: str | None = None) -> int:
    """该档位真实字号的**单行行高**（含行距），供单行标签设最小高度。"""
    fm = QFontMetrics(_token_font(label, token, font_size))
    return fm.height() + PAD_H
