"""滑动开关小部件（切片 0）。

二态、带滑块动画、随主题取色（用 `QPalette`，不写死颜色），用于「附加功能」总开关。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QAbstractButton

TRACK_W = 44
TRACK_H = 24
MARGIN = 2


class Switch(QAbstractButton):
    def __init__(self, checked: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(TRACK_W, TRACK_H)
        self._offset = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self.toggled.connect(self._animate)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = float(value)
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def sizeHint(self) -> QSize:
        return QSize(TRACK_W, TRACK_H)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.EnabledChange:
            self.setCursor(Qt.PointingHandCursor if self.isEnabled() else Qt.ArrowCursor)
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        radius = rect.height() / 2
        palette = self.palette()
        track = palette.highlight().color() if self.isChecked() else palette.mid().color()
        if not self.isEnabled():
            track.setAlpha(80)
        painter.setPen(Qt.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, radius, radius)
        diameter = rect.height() - 2 * MARGIN
        x = MARGIN + self._offset * (rect.width() - diameter - 2 * MARGIN)
        painter.setBrush(palette.base().color())
        painter.drawEllipse(QRectF(x, MARGIN, diameter, diameter))
        painter.end()
