"""应用入口。"""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from app.bootstrap import bootstrap
from gui.main_window import MainWindow
from gui.widgets.text_shortcuts import install_text_shortcuts


def _init_webengine() -> None:
    """Qt WebEngine 在创建 QApplication 前初始化（缺失则静默降级）。"""
    try:
        from PySide6.QtWebEngineQuick import QtWebEngineQuick

        QtWebEngineQuick.initialize()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _init_webengine()
    app = QApplication(sys.argv)
    app.setApplicationName("言明通")
    install_text_shortcuts()

    ctx = bootstrap()
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    # 先推首屏数据再显示：settings.state 会先应用持久化主题，避免亮/暗色闪现
    ctx.controller.push_initial_state()
    window.show()

    try:
        return app.exec()
    finally:
        ctx.worker.stop()


if __name__ == "__main__":
    raise SystemExit(main())
