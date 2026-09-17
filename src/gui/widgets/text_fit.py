"""让标签宽度跟上字号档位（docs 05 §1 视觉规范的收尾一步）。

**要解决的问题**：样式表里的 `font-size` 不参与 `sizeHint` 计算 ——
在居中或紧凑布局里，标签只会拿到「按默认字号估出来」的宽度，于是文本被裁。
实测（rev11）：空状态标题在标准档下文本需 252px，而布局只给 240px，
界面上就是「字显示不全」。

**为什么不写在 `gui.theme`**：`theme` 的契约是「只依赖标准库」（只算 token），
而这里必须用 Qt 的字体度量，故单列一个薄模块。字号仍**只从 `theme` 取**，不引入第二份来源。
"""

from __future__ import annotations

from PySide6.QtGui import QFont, QFontMetrics

from gui import theme

#: 文本两侧留白（与 QSS padding 无关，只用于抵消字形的左右边距）
PAD_PX = 8


def fit_label(label, token: str = "ui", font_size: str | None = None) -> int:
    """按该档位的真实像素字号量一遍文本，设为标签最小宽度；返回该宽度。

    调用时机：样式表已应用之后（`theme.apply(...)`），且每次**档位变化**都要重算，
    因为宽度依赖字号。返回宽度便于测试断言。
    """
    font = QFont(label.font())
    font.setPixelSize(theme.font_px(token, font_size))
    width = QFontMetrics(font).horizontalAdvance(label.text()) + PAD_PX
    label.setMinimumWidth(width)
    return width
