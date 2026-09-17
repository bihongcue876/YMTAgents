"""外观（主题 + 字号档位）单元测试，含 GUI 取色/取字号纪律静态检查（docs 05 §1）。

静态检查的目的：颜色字面量与字号字面量只允许存在于 `gui/theme.py`，
其余 GUI 模块一律经 `theme.palette()` / `theme.font_sizes()` / QSS 属性选择器取值——
否则主题或字号切换必然漏改。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from gui import theme

GUI = Path(__file__).resolve().parents[2] / "src" / "gui"
COLOR_LITERAL = re.compile(r"#[0-9A-Fa-f]{6}\b")
#: 「字号字面量」= 样式表里的 `font-size:`，或**值不来自 theme** 的直接设字号调用。
#: 后者原先按「调用名」判（`setPointSize|setPixelSize` 一律算违规），会误伤
#: 「按 theme.font_px 构造字体做文本度量」这类合法用法（如 `gui/widgets/text_fit.py`），
#: 故收紧为「实参不以 theme. 开头」才算违规 —— 规则本意是**单一来源**，不是禁用 API。
FONT_LITERAL = re.compile(r"font-size\s*:|set(?:Point|Pixel)Size\s*\(\s*(?!theme\.)")


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


# -- 字号档位 -----------------------------------------------------------------


def test_font_size_names_and_labels():
    assert theme.font_size_names() == ["small", "normal", "large", "xlarge"]
    assert theme.font_level("normal").label == "标准"
    assert theme.DEFAULT_FONT_SIZE == "normal"


def test_unknown_font_size_falls_back_to_normal():
    """未知 / 空值回退标准档（安全默认，docs 09 P6）。"""
    for bad in (None, "", "   ", "huge"):
        assert theme.font_level(bad).name == "normal"


def test_larger_level_means_larger_px():
    """档位单调：每个 token 的字号都随档位同向递增，不得出现某一项走反。"""
    order = theme.font_size_names()
    for token in theme.BASE_PX:
        sizes = [theme.font_px(token, n) for n in order]
        assert sizes == sorted(sizes), token
        assert sizes[0] < sizes[-1], token


def test_default_font_is_larger_than_baseline():
    """回归锚点：默认档字号必须**大于**Qt 系统默认（约 12px）与旧版正文 14px。

    用户要求「调大默认字号」——若默认档回落到 Qt 默认值或旧基准，本用例即失败。
    """
    normal = theme.font_sizes("normal")
    assert normal["ui"] >= 15
    assert normal["title"] >= 18
    assert normal["body"] >= 16
    assert normal["code"] >= 15
    assert normal["caption"] >= 14


def test_small_level_matches_legacy_sizes():
    """小档 ≈ 调整前的字号（正文 14 / 代码 13 / 小字 12 / 页标题 16），供需要旧密度者回退。"""
    small = theme.font_sizes("small")
    assert (small["body"], small["code"], small["caption"], small["title"]) == (14, 13, 12, 16)


def test_stylesheet_carries_font_size():
    qss = theme.stylesheet("light", "large")
    assert f"font-size: {theme.font_px('ui', 'large')}px" in qss
    assert f"font-size: {theme.font_px('title', 'large')}px" in qss
    assert "QLabel#pageTitle" in qss
    assert theme.stylesheet("light", "small") != qss


def test_markdown_css_carries_font_size():
    css = theme.markdown_css("dark", "xlarge")
    for token in ("body", "code", "caption"):
        assert f"font-size: {theme.font_px(token, 'xlarge')}px" in css


def test_apply_sets_font_size(qapp):
    try:
        theme.apply("light", "xlarge")
        assert f"font-size: {theme.font_px('ui', 'xlarge')}px" in qapp.styleSheet()
    finally:
        theme.apply(theme.DEFAULT_THEME, theme.DEFAULT_FONT_SIZE)  # 还原，避免影响同进程其它用例


def test_gui_has_no_hardcoded_font_size_literals():
    """字号纪律：GUI 内不得出现字号字面量（只允许在 gui/theme.py）。

    判据见 `FONT_LITERAL`：`font-size:` 或「值不来自 theme」的直接设字号调用。
    合法例外是 `gui/widgets/text_fit.py` —— 它按 `theme.font_px()` 构造字体做度量，
    字号仍取自单一来源。
    """
    offenders: list[str] = []
    for path in sorted(GUI.rglob("*.py")):
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FONT_LITERAL.search(line):
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, "字号字面量必须收敛到 gui/theme.py：\n" + "\n".join(offenders)


def test_ui_settings_font_size_matches_theme_levels():
    """settings.json 的 `ui.font_size` 取值集必须与 gui.theme.FONT_LEVELS 完全一致。"""
    from shared.schema import UISettings

    assert UISettings().font_size == theme.DEFAULT_FONT_SIZE
    assert set(UISettings.model_fields["font_size"].annotation.__args__) == set(theme.FONT_LEVELS)
    with pytest.raises(ValidationError):
        UISettings(font_size="huge")


def test_fit_label_grows_with_font_level(qapp):
    """回归锚点：样式表字号不参与 `sizeHint`，标签最小宽度必须按档位显式重算。

    否则居中 / 紧凑布局里的标题会被裁（实测空状态标题需 252px，实得 240px）。
    """
    from PySide6.QtWidgets import QLabel

    from gui.widgets.text_fit import fit_label

    label = QLabel("言明通 / YMTAgents")
    small = fit_label(label, "title", "small")
    assert label.minimumWidth() == small
    assert small > 0

    xlarge = fit_label(label, "title", "xlarge")
    assert xlarge > small  # 档位调大，最小宽度必须跟着变大
