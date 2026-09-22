"""终端页与 rail 接线（v0.0.5 / docs 05 §4④）。

两段：
1. **页面级**：卡片、状态徽标、监视缓冲、选中、输入信号、渲染惰性；
2. **窗口级**：bootstrap + MainWindow，用假 shell 注入解释器（不起真解释器），
   从 `controller.handle` 走 spawn → input → close，断言界面与事件接线。
"""

from __future__ import annotations

import pathlib
import sys
import time

from app import bootstrap as bootstrap_mod
from app import paths
from core.shell.process import Interpreter
from gui.main_window import MainWindow
from gui.pages.terminal import MAX_LINES, TerminalPage
from shared.envelope import ShellClose, ShellInput, ShellSpawn
from tests.mocks.gateway import MockGateway

FAKE = pathlib.Path(__file__).resolve().parents[1] / "mocks" / "fake_shell.py"


def _fake_interp(kind: str = "bash") -> Interpreter:
    return Interpreter(kind=kind, path=sys.executable, argv=(sys.executable, "-u", str(FAKE)))


def _shell(**over) -> dict:
    base = {
        "id": "s1",
        "kind": "powershell",
        "state": "ready",
        "cwd": "D:\\proj",
        "pid": 4242,
        "last_command": "git status",
        "created_at": 0.0,
        "last_used": 0.0,
        "session": "sess_1",
    }
    base.update(over)
    return base


def _labels(widget) -> list:
    from PySide6.QtWidgets import QLabel

    return widget.findChildren(QLabel)


def _texts(widget) -> str:
    return " ".join(label.text() for label in _labels(widget))


# -- 页面级 -------------------------------------------------------------------
def test_empty_state_gives_next_step(qapp):
    page = TerminalPage()
    page.show()
    qapp.processEvents()
    try:
        assert "尚未唤醒任何终端" in _texts(page)
        assert not page._input.isEnabled()  # 没有 shell 时不可输入
        assert not page._send.isEnabled()
    finally:
        page.close()


def test_boundary_statement_is_visible(qapp):
    """诚实边界（docs 07 §6.2）：shell 内命令的出网不受本进程白名单约束 —— 界面必须说清。"""
    page = TerminalPage()
    try:
        assert "白名单" in _texts(page)
    finally:
        page.close()


def test_cards_show_monitoring_fields(qapp):
    page = TerminalPage()
    page.set_session_titles({"sess_1": "重构会话"})
    page.show()
    qapp.processEvents()
    try:
        page.update_shells([_shell()], 5, "confirm", False)
        qapp.processEvents()
        texts = _texts(page)
        assert "s1" in texts and "powershell" in texts  # 标识与解释器
        assert "重构会话" in texts  # 所属会话（显示标题而非裸 id）
        assert "4242" in texts  # PID
        assert "D:\\proj" in texts  # cwd
        assert "git status" in texts  # 最近命令
        assert "最多 5 个" in page._summary.text()
        assert "默认拒绝" in page._summary.text()
        assert "confirm" in page._summary.text()  # 有效权限档可见
    finally:
        page.close()


def test_badge_state_drives_color_property(qapp):
    """状态徽标色由属性驱动（docs 05 §1），故属性必须随状态更新。

    每个状态用**新建页面**断言：`deleteLater` 的旧卡片要等事件循环回收，
    复用页面会读到上一轮已标记删除的徽标（测试自身的坑，不是产品行为）。
    """
    for state in ("starting", "ready", "busy", "dead"):
        page = TerminalPage()
        page.show()
        qapp.processEvents()
        try:
            page.update_shells([_shell(state=state)], 5, "confirm", False)
            qapp.processEvents()
            badges = [w for w in _labels(page) if w.objectName() == "shellBadge"]
            assert len(badges) == 1 and badges[0].property("shellState") == state
        finally:
            page.close()


def test_monitor_accumulates_output_and_is_lazy(qapp):
    """渲染惰性：页面不可见时不建渲染视图；可见后输出进缓冲并渲染。"""
    page = TerminalPage()
    page.update_shells([_shell()], 5, "confirm", False)
    assert page._monitor._view is None, "不可见时不得创建渲染视图（启动成本大头）"
    assert page._dirty

    page.resize(900, 600)
    page.show()
    qapp.processEvents()
    try:
        assert page._monitor._view is not None
        page.on_output("s1", "first line\n")
        page.on_output("s1", "second line\n")
        qapp.processEvents()
        assert "".join(page._buffers["s1"]) == "first line\nsecond line\n"
    finally:
        page.close()


def test_output_buffer_is_bounded(qapp):
    """长跑不膨胀：环形缓冲按行数封顶。"""
    page = TerminalPage()
    page.update_shells([_shell()], 5, "confirm", False)
    page.on_output("s1", "\n".join(f"row-{i}" for i in range(MAX_LINES + 500)))
    assert len(page._buffers["s1"]) <= MAX_LINES + 1


def test_output_for_removed_shell_is_dropped(qapp):
    page = TerminalPage()
    page.update_shells([_shell(), _shell(id="s2")], 5, "confirm", False)
    page.on_output("s2", "x\n")
    page.update_shells([_shell()], 5, "confirm", False)  # s2 已关闭
    assert "s2" not in page._buffers


def test_selection_and_input_signal(qapp):
    page = TerminalPage()
    page.show()
    qapp.processEvents()
    sent: list[tuple] = []
    page.input_requested.connect(lambda sid, cmd: sent.append((sid, cmd)))
    try:
        page.update_shells([_shell(), _shell(id="s2")], 5, "confirm", False)
        qapp.processEvents()
        assert page._selected == "s1"  # 默认选第一个
        page._select("s2")
        assert page._selected == "s2"
        page._input.setText("echo from-user")
        page._on_send_input()
        assert sent == [("s2", "echo from-user")]
        assert page._input.text() == ""
    finally:
        page.close()


def test_empty_input_is_not_sent(qapp):
    page = TerminalPage()
    page.show()
    qapp.processEvents()
    sent: list[tuple] = []
    page.input_requested.connect(lambda sid, cmd: sent.append((sid, cmd)))
    try:
        page.update_shells([_shell()], 5, "confirm", False)
        qapp.processEvents()
        page._input.setText("   ")
        page._on_send_input()
        assert sent == []
    finally:
        page.close()


def test_close_button_emits_request(qapp):
    page = TerminalPage()
    page.show()
    qapp.processEvents()
    closed: list[str] = []
    page.close_requested.connect(closed.append)
    try:
        page.update_shells([_shell()], 5, "confirm", False)
        qapp.processEvents()
        buttons = [w for w in page.findChildren(type(page._add)) if w.text() == "关闭"]
        assert buttons
        buttons[0].click()
        assert closed == ["s1"]
    finally:
        page.close()


def test_theme_change_is_idempotent_and_updates_state(qapp):
    page = TerminalPage()
    page.resize(900, 600)
    page.show()
    qapp.processEvents()
    try:
        page.update_shells([_shell()], 5, "confirm", False)
        page.on_output("s1", "\x1b[32mgreen\x1b[0m\n")
        qapp.processEvents()
        page.set_theme("dark", "xlarge")
        assert page._theme == "dark" and page._font_size == "xlarge"
        page.set_theme("dark", "xlarge")  # 幂等：同值不重复动作
        assert page._theme == "dark"
    finally:
        page.close()


# -- 窗口级 -------------------------------------------------------------------
def _window(tmp_path, monkeypatch):
    # 注入假 shell：不起真解释器，但整条链路（注册 → 请求 → 事件 → 页面）都是真的
    monkeypatch.setattr("core.shell.manager.detect", lambda kind="auto": _fake_interp())
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())
    return ctx, MainWindow(ctx.bridge, data_root=str(ctx.root))


def test_rail_has_terminal_entry_and_page_in_stack(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        from PySide6.QtWidgets import QPushButton

        assert any("终端" in b.text() for b in window.sidebar.findChildren(QPushButton))
        assert window.stack.indexOf(window.terminal_page) >= 0
        window.sidebar.open_terminal.emit()
        assert window.stack.currentWidget() is window.terminal_page
    finally:
        window.close()
        ctx.worker.stop()


def test_spawn_input_close_round_trip_updates_page(tmp_path, monkeypatch, qapp):
    """端到端：controller 入口 → 假 shell 子进程 → 事件 → 终端页。"""
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        window.show()
        window.stack.setCurrentWidget(window.terminal_page)
        qapp.processEvents()
        assert window.terminal_page._shells == []

        ctx.controller.handle(ShellSpawn())
        qapp.processEvents()
        assert [s["id"] for s in window.terminal_page._shells] == ["s1"]
        assert window.terminal_page._selected == "s1"
        assert window.terminal_page._max == 5

        ctx.controller.handle(ShellInput(id="s1", command="echo from-terminal"))
        qapp.processEvents()
        assert "from-terminal" in "".join(window.terminal_page._buffers.get("s1", []))

        ctx.controller.handle(ShellClose(id="s1"))
        qapp.processEvents()
        assert window.terminal_page._shells == []
        assert "s1" not in window.terminal_page._buffers
    finally:
        window.close()
        ctx.worker.stop()


def test_terminal_shows_session_title(tmp_path, monkeypatch, qapp):
    """卡片显示所属会话标题（session.index 接线）。"""
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        from shared.envelope import NewSession

        ctx.controller.push_initial_state()
        ctx.controller.handle(NewSession(title="终端归属"))
        ctx.controller.handle(ShellSpawn())
        window.show()
        window.stack.setCurrentWidget(window.terminal_page)
        qapp.processEvents()
        assert "终端归属" in _texts(window.terminal_page)
    finally:
        window.close()
        ctx.worker.stop()


def test_page_spawn_button_submits_request(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        window.show()
        window.stack.setCurrentWidget(window.terminal_page)
        qapp.processEvents()
        window.terminal_page.spawn_requested.emit()
        # 请求经核心线程异步处理，事件再回主线程 —— 等到**页面**也看到为止
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not window.terminal_page._shells:
            qapp.processEvents()
            time.sleep(0.02)
        assert ctx.shell_manager.list_status(), "「＋ 新建终端」必须真的建出一个 shell"
        assert window.terminal_page._shells, "shell.list 事件必须回到终端页"
    finally:
        window.close()
        ctx.worker.stop()


def test_shutdown_leaves_no_shells(tmp_path, monkeypatch, qapp):
    """退出收口：shell 是运行态，退出即全关（不留悬挂进程）。"""
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.handle(ShellSpawn())
        assert ctx.shell_manager.list_status()
        ctx.controller.shutdown()
        assert ctx.shell_manager.list_status() == []
    finally:
        window.close()
        ctx.worker.stop()
