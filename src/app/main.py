"""应用入口。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.bootstrap import bootstrap
from gui.main_window import MainWindow
from gui.widgets.text_shortcuts import install_text_shortcuts


def _app_icon() -> QIcon:
    """应用图标（rev35）：标题栏与任务栏共用 `src/feature/y-ico.ico`。

    源图可能是单张超大 PNG-in-ICO（无多尺寸条目）；显式补入 16–256 各档位，
    否则 Windows 任务栏取不到合适尺寸会回退成默认图标。
    """
    path = Path(__file__).resolve().parents[1] / "feature" / "y-ico.ico"
    if not path.exists():
        return QIcon()
    base = QIcon(str(path))
    icon = QIcon()
    src = base.pixmap(1024, 1024)
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(src.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return icon


def _set_taskbar_identity() -> None:
    """Windows：显式 AppUserModelID，让任务栏以窗口图标显示而非归到 python.exe。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "YanMingTong.YMTAgents"
        )
    except Exception:  # noqa: BLE001
        pass


def _init_webengine() -> None:
    """Qt WebEngine 在创建 QApplication 前初始化（缺失则静默降级）。"""
    try:
        from PySide6.QtWebEngineQuick import QtWebEngineQuick

        QtWebEngineQuick.initialize()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    # 日志装配在 bootstrap 内完成（级别取自 settings.logging.level，见 app.logging_setup）
    _set_taskbar_identity()
    _init_webengine()
    app = QApplication(sys.argv)
    app.setApplicationName("言明通")
    app.setWindowIcon(_app_icon())
    install_text_shortcuts()

    ctx = bootstrap()
    window = MainWindow(ctx.bridge, data_root=str(ctx.root))
    window.setWindowIcon(app.windowIcon())
    # 先推首屏数据再显示：settings.state 会先应用持久化主题，避免亮/暗色闪现
    ctx.controller.push_initial_state()
    window.show()

    try:
        return app.exec()
    finally:
        # 退出收口：停核心线程后结束当前会话（fsync 事件流，docs 03 §10）
        ctx.worker.stop()
        ctx.controller.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
