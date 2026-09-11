"""主题（亮 / 暗）token 与样式渲染（docs 05 §1 视觉规范）。

**单一取色来源**：GUI 各处一律经本模块取色，不得再出现颜色字面量
（`QApplication.setStyleSheet(stylesheet(...))` 定全局外观，消息流内联样式取 `palette(...)`）。

- `stylesheet(name)`：全应用 QSS，作用于 QApplication。
- `markdown_css(name)`：消息流 HTML 的颜色规则（结构性排版留在渲染模板内）。
- `pygments_style(name)`：代码高亮样式名。Pygments 输出的 class 名与样式无关，
  故高亮回调无需感知主题，只有 CSS 需要。

token 命名与 docs 05 §1 完全一致；暗色是同名 token 的变体，语义不变。
本模块只依赖标准库。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_THEME = "light"


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


def pygments_style(name: str | None) -> str:
    return "monokai" if palette(name).name == DARK.name else "default"


def stylesheet(name: str | None) -> str:
    """全应用 QSS。以 token 渲染，主题切换即整体换肤。"""
    p = palette(name)
    return f"""
QWidget {{ background: {p.bg}; color: {p.fg}; }}
QMainWindow, QDialog {{ background: {p.bg}; }}
QLabel {{ background: transparent; }}
QToolTip {{ background: {p.surface}; color: {p.fg}; border: 1px solid {p.border}; }}

QPushButton {{
    background: {p.surface}; color: {p.fg};
    border: 1px solid {p.border}; border-radius: 6px; padding: 5px 12px;
}}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton:pressed {{ background: {p.accent}; color: {p.bg}; }}
QPushButton:disabled {{ color: {p.muted}; }}

QLineEdit, QSpinBox, QComboBox, QTextEdit, QPlainTextEdit, QTextBrowser {{
    background: {p.surface}; color: {p.fg};
    border: 1px solid {p.border}; border-radius: 6px; padding: 4px 6px;
    selection-background-color: {p.accent}; selection-color: {p.bg};
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


def markdown_css(name: str | None) -> str:
    """消息流 HTML 的颜色规则（排版规则见渲染模板）。"""
    p = palette(name)
    return f"""
body {{ color: {p.fg}; background: {p.bg}; }}
pre {{ background: {p.code_bg}; }}
th, td {{ border-color: {p.border}; }}
blockquote {{ border-left-color: {p.border}; color: {p.muted}; }}
a {{ color: {p.accent}; }}
.user .bubble {{ background: {p.surface}; }}
.usage, .tag {{ color: {p.muted}; }}
.error {{ background: {p.danger_bg}; border-color: {p.danger_border}; color: {p.danger}; }}
"""


def apply(name: str | None) -> str:
    """把主题 QSS 应用到当前 QApplication，返回**实际生效**的主题名。

    Qt 为延迟导入：本模块在无 Qt 环境（如纯渲染单测）下仍可导入。
    """
    from PySide6.QtWidgets import QApplication

    p = palette(name)
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(stylesheet(p.name))
    return p.name
