"""外观（亮/暗主题 + 字号档位）token 与样式渲染（docs 05 §1 视觉规范）。

**单一取色来源、单一取字号来源**：GUI 各处一律经本模块取色与取字号，
不得再出现颜色字面量或字号字面量（`QApplication.setStyleSheet(stylesheet(...))` 定全局外观，
消息流内联样式取 `palette(...)`，字号取 `font_sizes(...)` / `font_px(...)`）。

- `stylesheet(name, font_size)`：全应用 QSS，作用于 QApplication。
- `markdown_css(name, font_size)`：消息流 HTML 的颜色与字号规则
  （字体族、行高、间距等结构性排版留在渲染模板内）。
- `pygments_style(name)`：代码高亮样式名。Pygments 输出的 class 名与样式无关，
  故高亮回调无需感知主题，只有 CSS 需要。

token 命名与 docs 05 §1 完全一致；暗色是同名 token 的变体，语义不变；
字号是**基准表 × 档位系数**，故调档位不会打乱文字层级。
本模块只依赖标准库。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_THEME = "light"
DEFAULT_FONT_SIZE = "normal"


@dataclass(frozen=True)
class Palette:
    name: str
    label: str
    bg: str
    surface: str
    fg: str
    muted: str
    border: str
    accent: str
    ok: str
    warn: str
    danger: str
    danger_bg: str
    danger_border: str
    code_bg: str


LIGHT = Palette(
    name="light",
    label="亮色",
    bg="#FFFFFF",
    surface="#F7F8FA",
    fg="#1F2328",
    muted="#6B7280",
    border="#E5E7EB",
    accent="#2563EB",
    ok="#16A34A",
    warn="#D97706",
    danger="#DC2626",
    danger_bg="#FEF2F2",
    danger_border="#FECACA",
    code_bg="#F7F8FA",
)

DARK = Palette(
    name="dark",
    label="暗色",
    bg="#1E1F22",
    surface="#26282C",
    fg="#E8EAED",
    muted="#9AA0A6",
    border="#3C4043",
    accent="#8AB4F8",
    ok="#5DD58A",
    warn="#F5B544",
    danger="#F28B82",
    danger_bg="#3A2422",
    danger_border="#7A3B36",
    code_bg="#2A2C30",
)

PALETTES: dict[str, Palette] = {LIGHT.name: LIGHT, DARK.name: DARK}


def theme_names() -> list[str]:
    """可选主题名（顺序即界面下拉顺序）。"""
    return list(PALETTES)


def palette(name: str | None) -> Palette:
    """取色板；未知或空值回退亮色（安全默认，docs 09 P6）。"""
    return PALETTES.get((name or DEFAULT_THEME).strip().lower(), LIGHT)


# ---------------------------------------------------------------------------
# 字号档位（docs 05 §1「字体与版式」）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FontLevel:
    name: str
    label: str
    scale: float


#: 可选档位；顺序即界面下拉顺序。`normal` 为默认档，其绝对值即「已调大」后的基准。
FONT_LEVELS: dict[str, FontLevel] = {
    "small": FontLevel("small", "小", 0.875),
    "normal": FontLevel("normal", "标准", 1.0),
    "large": FontLevel("large", "大", 1.15),
    "xlarge": FontLevel("xlarge", "特大", 1.30),
}

#: 基准字号（normal 档，px）。整表按档位系数缩放，层级关系恒定。
#: 说明：`ui` 为界面通用文字，调档前 Qt 系统默认约 12px，故基准即已上抬。
BASE_PX: dict[str, int] = {
    "title": 18,  # 页面标题
    "ui": 15,  # 界面通用文字（按钮/列表/输入框/表单）
    "body": 16,  # 消息正文
    "code": 15,  # 代码等宽
    "caption": 14,  # 用量、标签等小字
}


def font_size_names() -> list[str]:
    """可选字号档位名（顺序即界面下拉顺序）。"""
    return list(FONT_LEVELS)


def font_level(name: str | None) -> FontLevel:
    """取档位；未知或空值回退标准档（安全默认，docs 09 P6）。"""
    return FONT_LEVELS.get(
        (name or DEFAULT_FONT_SIZE).strip().lower(), FONT_LEVELS[DEFAULT_FONT_SIZE]
    )


def font_sizes(name: str | None = None) -> dict[str, int]:
    """该档位下的全部字号（px），键与 `BASE_PX` 一致。"""
    scale = font_level(name).scale
    return {token: max(1, round(px * scale)) for token, px in BASE_PX.items()}


def font_px(token: str, name: str | None = None) -> int:
    """单个 token 在该档位下的字号（px）。"""
    return font_sizes(name)[token]


def pygments_style(name: str | None) -> str:
    return "monokai" if palette(name).name == DARK.name else "default"


def stylesheet(name: str | None, font_size: str | None = None) -> str:
    """全应用 QSS。以 token 渲染，主题或字号切换即整体换肤/重排。"""
    p = palette(name)
    fs = font_sizes(font_size)
    return f"""
QWidget {{ background: {p.bg}; color: {p.fg}; font-size: {fs["ui"]}px; }}
QMainWindow, QDialog {{ background: {p.bg}; }}
QLabel {{ background: transparent; }}
QLabel#pageTitle {{ font-size: {fs["title"]}px; font-weight: 600; }}
QLabel#emptyTitle {{ font-size: {fs["title"]}px; font-weight: 600; color: {p.fg}; }}
QLabel#emptyHint {{ color: {p.muted}; }}
QToolTip {{ background: {p.surface}; color: {p.fg}; border: 1px solid {p.border}; }}

QPushButton {{
    background: {p.surface}; color: {p.fg};
    border: 1px solid {p.border}; border-radius: 6px; padding: 5px 12px;
}}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton:pressed {{ background: {p.accent}; color: {p.bg}; }}
QPushButton:disabled {{ color: {p.muted}; }}

/* 侧栏图标栏（rail）：无边框扁平按钮，折叠后仍常显（rev13） */
QPushButton#railButton {{
    background: transparent; color: {p.fg};
    border: none; border-radius: 8px; font-size: 16px;
}}
QPushButton#railButton:hover {{ background: {p.surface}; }}
QPushButton#railButton:pressed {{ background: {p.accent}; color: {p.bg}; }}

/* 侧栏分隔条：默认隐形，hover 显形提示可拖拽 */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {p.border}; }}
QSplitter::handle:horizontal {{ width: 4px; }}

QLineEdit, QSpinBox, QComboBox, QTextEdit, QPlainTextEdit, QTextBrowser {{
    background: {p.surface}; color: {p.fg};
    border: 1px solid {p.border}; border-radius: 6px; padding: 4px 6px;
    selection-background-color: {p.accent}; selection-color: {p.bg};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {p.muted}; background: {p.bg}; border-color: {p.border};
}}
QComboBox::drop-down {{ border: 0; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {p.surface}; color: {p.fg};
    border: 1px solid {p.border};
    selection-background-color: {p.accent}; selection-color: {p.bg};
}}

QListWidget, QTableView, QTableWidget, QTreeWidget {{
    background: {p.surface}; color: {p.fg};
    border: 1px solid {p.border}; border-radius: 6px;
    alternate-background-color: {p.bg};
}}
QListWidget::item, QTableView::item, QTableWidget::item {{ padding: 3px 4px; }}
QListWidget::item:selected, QTableView::item:selected, QTableWidget::item:selected {{
    background: {p.accent}; color: {p.bg};
}}
QListWidget::item:hover, QTableView::item:hover, QTableWidget::item:hover {{
    background: {p.border};
}}
QHeaderView::section {{
    background: {p.bg}; color: {p.muted};
    border: 0; border-bottom: 1px solid {p.border}; padding: 4px 6px;
}}
QTableCornerButton::section {{ background: {p.bg}; border: 0; }}

QMenu {{ background: {p.surface}; color: {p.fg}; border: 1px solid {p.border}; }}
QMenu::item:selected {{ background: {p.accent}; color: {p.bg}; }}
QMenuBar {{ background: {p.bg}; }}

QCheckBox, QRadioButton, QGroupBox {{ background: transparent; color: {p.fg}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {p.muted}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 5px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {p.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: {p.border}; }}
QProgressBar {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 6px; }}
QProgressBar::chunk {{ background: {p.accent}; }}

/* 状态色：由属性驱动，主题切换即整体换色（勿在控件上写死颜色） */
QLabel#keyBadge[keyStored="true"], QLabel#testResult[testOk="true"] {{ color: {p.ok}; }}
QLabel#keyBadge[keyStored="false"], QLabel#testResult[testOk="false"] {{ color: {p.danger}; }}
QLabel#fetchResult {{ color: {p.muted}; }}
QLabel#fetchResult[fetchOk="true"] {{ color: {p.ok}; }}
QLabel#fetchResult[fetchOk="false"] {{ color: {p.danger}; }}
"""


def restyle(*widgets) -> None:
    """属性变更后强制重算 QSS。

    Qt 不会自动重算属性选择器（`[prop="true"]`），改属性后须 unpolish/polish。
    """
    for w in widgets:
        if w is None:
            continue
        w.style().unpolish(w)
        w.style().polish(w)


# ANSI SGR 色码 → 颜色。亮色沿用惯例值；暗色重映射黑/白两端（30/37/90/97），
# 否则近黑色前景在深底上不可见。
_ANSI_LIGHT: dict[str, str] = {
    "30": "#1F2328", "31": "#DC2626", "32": "#16A34A", "33": "#D97706",
    "34": "#2563EB", "35": "#9333EA", "36": "#0891B2", "37": "#6B7280",
    "90": "#6B7280", "91": "#DC2626", "92": "#16A34A", "93": "#D97706",
    "94": "#2563EB", "95": "#9333EA", "96": "#0891B2", "97": "#1F2328",
}


def ansi_colors(name: str | None) -> dict[str, str]:
    """ANSI SGR 色码表（主题感知）。"""
    p = palette(name)
    colors = dict(_ANSI_LIGHT)
    if p.name == DARK.name:
        colors.update(
            {
                "30": p.fg, "37": p.muted, "90": p.muted, "97": p.fg,
                "31": p.danger, "32": p.ok, "33": p.warn, "34": p.accent,
                "91": p.danger, "92": p.ok, "93": p.warn, "94": p.accent,
            }
        )
    return colors


def markdown_css(name: str | None, font_size: str | None = None) -> str:
    """消息流 HTML 的颜色与字号规则（字体族、间距等结构性排版见渲染模板）。"""
    p = palette(name)
    fs = font_sizes(font_size)
    return f"""
body {{ color: {p.fg}; background: {p.bg}; font-size: {fs["body"]}px; }}
pre, code {{ font-size: {fs["code"]}px; }}
pre {{ background: {p.code_bg}; }}
th, td {{ border-color: {p.border}; }}
blockquote {{ border-left-color: {p.border}; color: {p.muted}; }}
a {{ color: {p.accent}; }}
.user .bubble {{ background: {p.surface}; }}
.usage, .tag {{ color: {p.muted}; font-size: {fs["caption"]}px; }}
.error {{ background: {p.danger_bg}; border-color: {p.danger_border}; color: {p.danger}; }}
"""


def apply(name: str | None, font_size: str | None = None) -> str:
    """把外观（QSS + **调色板**）应用到当前 QApplication，返回**实际生效**的主题名。

    QSS 覆盖控件样式，但有一批绘制**不走 QSS**、直接取调色板
    （QFrame 边框、禁用态、微调框箭头等）—— 只换 QSS 不换调色板，
    暗色下就会残留亮灰元素（spec rev12 §1）。故此处两者一起应用。

    Qt 为延迟导入：本模块在无 Qt 环境（如纯渲染单测）下仍可导入。
    """
    from PySide6.QtWidgets import QApplication

    from gui.app_palette import apply as apply_palette

    p = palette(name)
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(stylesheet(p.name, font_size))
        apply_palette(p.name)
    return p.name
