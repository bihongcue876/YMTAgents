"""边界关卡：UI ↔ 核的唯一通道（spec §2.1 / docs 02 §1）。

- GUI 线程调用 `submit()` 投递请求（非阻塞）；核心线程用 `get()` 取出处理。
- 核心线程调用 `emit_event()` 发射事件；Qt 队列连接自动投递回 GUI 线程。
- 请求校验失败立即回 ErrorReport(invalid_request)。
- GUI 不持有任何 core 内部对象引用，一切通信仅经本对象。
"""

from __future__ import annotations

import logging
import queue

from pydantic import ValidationError
from PySide6.QtCore import QObject, Signal

from shared.envelope import ErrorReport, parse_request

log = logging.getLogger(__name__)


class BusBridge(QObject):
    event_received = Signal(object)  # Event

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: queue.Queue = queue.Queue()

    def submit(self, request: object) -> None:
        """GUI 线程调用；非阻塞，立即返回。"""
        try:
            if isinstance(request, dict):
                request = parse_request(request)
        except ValidationError as exc:
            self.emit_event(
                ErrorReport(
                    scope="system",
                    code="invalid_request",
                    message="请求格式不合法",
                    detail=str(exc)[:500],
                )
            )
            return
        self._queue.put(request)

    def get(self, timeout: float = 0.1):
        """核心线程调用；超时返回 None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def emit_event(self, event: object) -> None:
        """核心线程调用；经信号投递回 GUI 线程。"""
        self.event_received.emit(event)
