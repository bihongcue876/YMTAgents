"""对话视图空状态（rev2 §1.3 / 验收 A1）。

规则：居中显示 应用名 + 一句话说明 + 一个 CTA；
无供应商时 CTA = 「添加模型」（跳模型配置页），已有供应商时 = 「开始对话」（聚焦输入区）。
禁止白屏式失败：任何空状态都必须给出下一步（docs 05 §5.4）。

补充（spec rev9 §6）：此前全项目**没有任何空状态实现**，而 BUILD 把 A1 记为通过；
本模块是那次「验收与实现不符」的修复。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from gui.widgets import text_fit

APP_NAME = "言明通 / YMTAgents"
# rev16：去掉自我定位式描述（「超级 Agent」这类介绍不该由应用自己说），
# 只留可验证的事实陈述；身份定位交给 persona 轮的 YMT 角色去定义。
TAGLINE = "本地优先 —— 会话、密钥与数据都留在本机。"
HINT_NO_PROVIDER = "还没有可用模型：点「添加模型」——云端选预设并填 API Key，本地服务（Ollama / LM Studio）无需密钥。"
HINT_READY = "模型已就绪，在下方输入框开始对话。"


class EmptyState(QWidget):
    """居中引导：应用名 + 当前该做的一步 + 一个按钮。"""

    add_model = Signal()
    start_chat = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._has_provider = False
        self._font_size: str | None = None

        title = QLabel(APP_NAME)
        title.setObjectName("emptyTitle")
        self._title = title
        tagline = QLabel(TAGLINE)
        tagline.setObjectName("emptyHint")
        tagline.setAlignment(Qt.AlignCenter)
        tagline.setWordWrap(True)
        self._tagline = tagline

        self._hint = QLabel(HINT_NO_PROVIDER)
        self._hint.setObjectName("emptyHint")
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setWordWrap(True)

        self._cta = QPushButton("添加模型")
        self._cta.clicked.connect(self._on_cta)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(title, alignment=Qt.AlignCenter)
        layout.addWidget(tagline, alignment=Qt.AlignCenter)
        layout.addSpacing(8)
        layout.addWidget(self._hint, alignment=Qt.AlignCenter)
        layout.addWidget(self._cta, alignment=Qt.AlignCenter)
        layout.addStretch(1)

    def set_has_provider(self, has_provider: bool) -> None:
        """按「是否已有供应商」切换引导文案与 CTA（rev2 §1.3 的两种空状态）。"""
        self._has_provider = bool(has_provider)
        self._hint.setText(HINT_READY if self._has_provider else HINT_NO_PROVIDER)
        self._cta.setText("开始对话" if self._has_provider else "添加模型")
        self._fit_vertical()

    def cta_text(self) -> str:
        """当前 CTA 文案（供测试断言两种空状态）。"""
        return self._cta.text()

    def refresh_metrics(self, font_size: str | None = None) -> None:
        """按当前字号档位重算标签的宽与高（横向 rev11 / 纵向 rev16）。

        样式表字号不参与 `sizeHint`：居中布局下标签只拿到「按默认字号估」的宽**与高**，
        横向被省略、纵向被腰斩。故每次档位变化都要重算一次。
        """
        self._font_size = font_size
        text_fit.fit_label(self._title, "title", font_size)
        self._fit_vertical()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 宽度变化会改变换行行数 → 高度必须跟着重算
        super().resizeEvent(event)
        self._fit_vertical()

    def _fit_vertical(self) -> None:
        self._title.setMinimumHeight(text_fit.line_height(self._title, "title", self._font_size))
        text_fit.fit_wrapped_label(self._tagline, TAGLINE, "ui", self._font_size)
        text_fit.fit_wrapped_label(self._hint, self._hint.text(), "ui", self._font_size)

    def _on_cta(self) -> None:
        (self.start_chat if self._has_provider else self.add_model).emit()
