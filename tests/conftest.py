"""pytest 全局配置：离屏 GUI、禁 WebEngine、核心线程自动回收。

**为什么要有 `_reap_core_workers`**：`bootstrap()` 会起一个 `CoreWorker` QThread。
个别用例创建了上下文却没 `worker.stop()` —— 解释器退出时 Qt 发现「仍运行的 QThread」，
直接 fast-fail（实测退出码 0xC0000409 / 127，而 pytest 报 170 passed，极具迷惑性）。
产品侧退出是干净的（`app/main.py` 的 finally 有 stop + shutdown），
故这是**测试卫生**问题：此 fixture 统一回收，含未来新增用例。
"""

from __future__ import annotations

import contextlib
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("YMT_NO_WEBENGINE", "1")


@pytest.fixture(autouse=True)
def _reap_core_workers(monkeypatch):
    """每个用例结束后，停掉本用例创建的全部 CoreWorker。"""
    try:
        from app.core_thread import CoreWorker
    except Exception:  # noqa: BLE001 - 无 Qt 环境下不介入
        yield
        return

    created: list = []
    original_init = CoreWorker.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(CoreWorker, "__init__", tracking_init)
    yield
    for worker in created:
        with contextlib.suppress(Exception):  # 回收失败不得掩盖真实断言失败
            worker.stop()
