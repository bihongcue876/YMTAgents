"""核心工作线程（docs 04 §2）。

- Qt 主线程只渲染；本线程处理回合、模型调用、工具执行、文件写入。
- 从 BusBridge 队列取请求，交由 handler 处理；异常不使进程崩溃。
- 首期单线程、FIFO、无优先级。
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QThread

from core.bus.bridge import BusBridge

log = logging.getLogger(__name__)


class CoreWorker(QThread):
    def __init__(
        self,
        bridge: BusBridge,
        handler: Callable[[object], None],
        parent=None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.handler = handler
        self.on_stop = on_stop
        self._running = True

    def run(self) -> None:
        try:
            while self._running:
                request = self.bridge.get(timeout=0.1)
                if request is None:
                    continue
                try:
                    self.handler(request)
                except Exception:  # noqa: BLE001 - 单请求失败不影响后续
                    log.exception("处理请求失败：%s", getattr(request, "type", "?"))
        finally:
            if self.on_stop is not None:
                try:
                    self.on_stop()
                except Exception:  # noqa: BLE001 - 清理失败不得杀穿 Qt 退出
                    log.exception("核心线程关闭清理失败")

    def stop(self) -> None:
        self._running = False
        self.wait(2000)
