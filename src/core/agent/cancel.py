"""协作式取消令牌（docs 04 §2）。

检查点固定三处：流式增量之间、工具执行前、确认等待恢复后。
"""

from __future__ import annotations

import threading


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()
