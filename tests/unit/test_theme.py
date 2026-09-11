"""主题（亮 / 暗）单元测试，含 GUI 取色纪律静态检查（docs 05 §1）。

静态检查的目的：颜色字面量只允许存在于 `gui/theme.py`，
其余 GUI 模块一律经 `theme.palette()` / QSS 属性选择器取色——否则主题切换必然漏改。
"""

from __future__ import annotations

import re
from pathlib import Path

from gui import theme

GUI = Path(__file__).resolve().parents[2] / "src" / "gui"
COLOR_LITERAL = re.compile(r"#[0-9A-Fa-f]{6}\b")


def test_theme_names_and_labels():
    assert theme.theme_names() == ["light", "dark"]
    assert theme.palette("light").label == "亮色"
    assert theme.palette("dark").label == "暗色"


def test_unknown_theme_falls_back_to_light():
    """未知 / 空值回退亮色（安全默认，docs 09 P6）。"""
    for bad in (None, "", "   ", "solarized"):
        assert theme.palette(bad).name == "light"


def test_stylesheets_differ_and_carry_token_values():
    light = theme.stylesheet("light")
    dark = theme.stylesheet("dark")
    assert light != dark
    assert theme.palette("light").bg in light
    assert theme.palette("dark").bg in dark
    assert theme.palette("dark").fg in dark


def test_markdown_css_carries_colors():
    css = theme.markdown_css("dark")
    p = theme.palette("dark")
    for token in (p.fg, p.bg, p.accent, p.danger):
        assert token in css


def test_pygments_style_follows_theme():
    assert theme.pygments_style("light") != theme.pygments_style("dark")


def test_ansi_colors_remap_dark_black_and_white():
    """回归锚点：ANSI「黑」(30) 在暗色下必须提亮，否则深底上看不见。"""
    light = theme.ansi_colors("light")
    dark = theme.ansi_colors("dark")
    assert light["30"] == "#1F2328"
    assert dark["30"] == theme.palette("dark").fg
    assert dark["30"] != light["30"]
    assert dark["37"] == theme.palette("dark").muted


def test_apply_sets_application_stylesheet(qapp):
    try:
        used = theme.apply("dark")
        assert used == "dark"
        assert theme.palette("dark").bg in qapp.styleSheet()
    finally:
        theme.apply(theme.DEFAULT_THEME)  # 还原，避免影响同进程其它用例


def test_gui_has_no_hardcoded_color_literals():
    """取色纪律：GUI 内不得出现颜色字面量（只允许在 gui/theme.py）。"""
    offenders: list[str] = []
    for path in sorted(GUI.rglob("*.py")):
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if COLOR_LITERAL.search(line):
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, "颜色字面量必须收敛到 gui/theme.py：\n" + "\n".join(offenders)
