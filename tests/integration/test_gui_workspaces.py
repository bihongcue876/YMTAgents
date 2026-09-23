"""工作区界面（v0.0.6）：侧栏分组折叠 + 工作区页。

覆盖三层：
1. **页面级** —— 卡片徽标、文件列表缩进、截断提示、常驻边界声明、对话框负载与知情门；
2. **侧栏级** —— 分组、折叠、`select()` 在分组后仍能定位、无工作区数据时的平铺退化；
3. **窗口级** —— bootstrap + MainWindow，从 `controller.handle` 走 create / collapse / detail，
   断言界面真的跟着变（验证事件接线，不只是「页面自己会画」）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLabel, QListWidget, QMessageBox, QPushButton

from app import bootstrap as bootstrap_mod
from app import paths
from gui.main_window import MainWindow
from gui.pages.workspaces import NOTICE, WorkspaceDialog, WorkspacesPage
from gui.sidebar import Sidebar
from shared.envelope import (
    NewSession,
    WorkspaceCreate,
    WorkspaceDetail,
    WorkspaceDetailResult,
)
from shared.ids import WS_DEFAULT
from tests.mocks.gateway import MockGateway


def _info(**over) -> dict:
    base = {
        "id": "ws_1",
        "name": "我的项目",
        "builtin": False,
        "current": False,
        "root_kind": "external",
        "data_home_kind": "inline",
        "root": "D:\\proj",
        "data_home": "D:\\proj\\.ymtdata",
        "migratable": False,
        "missing": False,
        "sessions": 0,
        "build_cmd": "",
        "note": None,
    }
    base.update(over)
    return base


class _Meta:
    """最小会话元数据替身：侧栏只用到这几个字段。"""

    def __init__(self, sid: str, title: str, workspace_id=None, state="active") -> None:
        self.id = sid
        self.title = title
        self.workspace_id = workspace_id
        self.state = state
        self.updated_at = datetime.now(timezone.utc)


def _labels(widget) -> list[str]:
    return [w.text() for w in widget.findChildren(QLabel)]


def _items(widget) -> list[str]:
    out: list[str] = []
    for lst in widget.findChildren(QListWidget):
        out.extend(lst.item(i).text() for i in range(lst.count()))
    return out


def _head(widget, name: str) -> QPushButton:
    for button in widget.findChildren(QPushButton):
        if button.objectName() == "wsGroupHeader" and name in button.text():
            return button
    raise AssertionError(f"未找到分组头：{name}")


def _flat_list_items(widget) -> list[str]:
    """只取**平铺**列表（无分组头在它前面）的内容 —— 退化路径没有分组 widget 包着。"""
    return _items(widget)


# -- 页面级 -------------------------------------------------------------------
def test_page_renders_card_with_badges(qapp):
    page = WorkspacesPage()
    page.update_workspaces(
        [
            _info(id=WS_DEFAULT, name="默认工作区", builtin=True, current=True, migratable=True,
                  root="D:\\data\\workspace", data_home="D:\\data\\workspace"),
            _info(missing=True),
        ],
        WS_DEFAULT,
    )
    texts = _labels(page)
    assert any("默认工作区" in t for t in texts)
    for badge in ("内置", "当前", "目录不存在", "不可迁移", "可迁移"):
        assert any(badge in t for t in texts), badge


def test_page_always_shows_boundary_notice(qapp):
    """R6：边界声明必须常驻 —— 不因「工作区」二字给用户虚假安全感。"""
    page = WorkspacesPage()
    assert any(NOTICE in t for t in _labels(page))
    assert "安全边界" in NOTICE
    assert "不会删除磁盘上的任何文件" in NOTICE


def test_page_empty_state_hint(qapp):
    page = WorkspacesPage()
    page.update_workspaces([], "")
    assert any("还没有工作区" in t for t in _labels(page))


def test_page_card_disables_remove_for_builtin(qapp):
    page = WorkspacesPage()
    page.update_workspaces(
        [_info(id=WS_DEFAULT, name="默认工作区", builtin=True, current=True)], WS_DEFAULT
    )
    buttons = {b.text(): b for b in page.findChildren(QPushButton)}
    assert buttons["移除登记"].isEnabled() is False
    assert buttons["设为当前"].isEnabled() is False  # 已是当前


def test_page_file_list_indents_and_reports_truncation(qapp):
    page = WorkspacesPage()
    page.on_detail(
        WorkspaceDetailResult(
            id="ws_1",
            root="D:\\proj",
            entries=[
                {"path": "src", "name": "src", "dir": True, "size": 0},
                {"path": "src/main.py", "name": "main.py", "dir": False, "size": 12},
            ],
            truncated=True,
        )
    )
    items = _items(page)
    assert any("src" in t for t in items)
    assert any("main.py" in t and t.startswith("    ") for t in items)  # 层级缩进
    assert any("已达上限" in t for t in _labels(page))


def test_page_file_list_reports_error(qapp):
    page = WorkspacesPage()
    page.on_detail(
        WorkspaceDetailResult(id="ws_1", error="目录不存在（可能已被移动或删除）：D:\\gone")
    )
    assert any("目录不存在" in t for t in _labels(page))
    assert _items(page) == []


def test_dialog_requires_awareness_for_external_location(qapp, monkeypatch):
    """R1：外部目录必须经知情确认才放行 —— 未勾选时不得产出可提交的负载。"""
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(" ".join(map(str, a[1:]))))

    dialog = WorkspaceDialog(None, None, "inline")
    dialog._name.setText("外部项目")
    dialog._external.setChecked(True)
    dialog._dir.setText("D:\\proj")
    assert dialog._aware.isChecked() is False
    dialog._on_accept()
    assert warned and "知情确认" in warned[0]
    assert dialog.result() != QDialog.Accepted

    dialog._aware.setChecked(True)
    dialog._on_accept()
    payload = dialog.payload()
    assert payload["root_kind"] == "external"
    assert payload["root"] == "D:\\proj"
    assert payload["data_home_kind"] == "inline"  # D3 默认档


def test_dialog_requires_name(qapp, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(" ".join(map(str, a[1:]))))
    dialog = WorkspaceDialog(None, None, "inline")
    dialog._name.setText("   ")
    dialog._on_accept()
    assert warned and "名称" in warned[0]


def test_dialog_editing_locks_location_fields(qapp):
    """编辑态不许改位置与落点：更换目录只在创建时决定（单一入口，避免两处不一致的知情确认）。"""
    dialog = WorkspaceDialog(None, _info(), "inline")
    assert dialog._managed.isEnabled() is False
    assert dialog._external.isEnabled() is False
    assert dialog._home_kind.isEnabled() is False


# -- 侧栏级 -------------------------------------------------------------------
def test_sidebar_groups_sessions_by_workspace(qapp):
    bar = Sidebar()
    bar.update_workspaces(
        [
            _info(id=WS_DEFAULT, name="默认工作区", builtin=True, migratable=True),
            _info(id="ws_1", name="我的项目"),
        ],
        WS_DEFAULT,
        [],
    )
    bar.update_sessions([_Meta("s1", "甲会话"), _Meta("s2", "乙会话", workspace_id="ws_1")])
    items = _items(bar)
    assert any("甲会话" in t for t in items)
    assert any("乙会话" in t for t in items)
    assert "(1)" in _head(bar, "默认工作区").text()
    assert "(1)" in _head(bar, "我的项目").text()


def test_sidebar_collapse_hides_group_and_emits_signal(qapp):
    bar = Sidebar()
    bar.update_workspaces([_info(id=WS_DEFAULT, name="默认工作区", builtin=True)], WS_DEFAULT, [])
    bar.update_sessions([_Meta("s1", "甲会话")])
    seen: list[tuple] = []
    bar.collapse_workspace.connect(lambda wid, col: seen.append((wid, col)))

    _head(bar, "默认工作区").click()  # 当前未折叠 → 请求折叠
    assert seen == [(WS_DEFAULT, True)]  # 信号同步发出（契约不变，rev58）

    bar.update_workspaces(
        [_info(id=WS_DEFAULT, name="默认工作区", builtin=True)], WS_DEFAULT, [WS_DEFAULT]
    )
    assert not any("甲会话" in t for t in _items(bar))
    assert "▸" in _head(bar, "默认工作区").text()
    assert "(1)" in _head(bar, "默认工作区").text()  # 计数仍在，只是会话列表收起

    _head(bar, "默认工作区").click()
    assert seen[-1] == (WS_DEFAULT, False)


def test_sidebar_select_finds_session_after_grouping(qapp):
    """分组后 `select()` 仍要能定位 —— 分组改造最容易打断的就是这条。"""
    bar = Sidebar()
    bar.update_workspaces([_info(id="ws_1", name="我的项目")], "ws_1", [])
    bar.update_sessions([_Meta("s1", "甲会话", workspace_id="ws_1")])
    bar.select("s1")
    assert any(
        lst.currentItem() is not None and lst.currentItem().data(Qt.UserRole) == "s1"
        for lst in bar.findChildren(QListWidget)
    )


def test_sidebar_degrades_to_flat_list_before_workspaces_arrive(qapp):
    """首帧只有 session.index 时退化为平铺 —— 不空白、不误分组。"""
    bar = Sidebar()
    bar.update_sessions([_Meta("s1", "甲会话")])
    assert any("甲会话" in t for t in _flat_list_items(bar))
    assert not any(
        b.objectName() == "wsGroupHeader" and "甲会话" in b.text()
        for b in bar.findChildren(QPushButton)
    )


def test_sidebar_archived_toggle_groups_archived(qapp):
    bar = Sidebar()
    bar.update_workspaces([_info(id=WS_DEFAULT, name="默认工作区", builtin=True)], WS_DEFAULT, [])
    bar.update_sessions([_Meta("s1", "活跃"), _Meta("s2", "归档了", state="archived")])
    assert any("已归档 (1)" in b.text() for b in bar.findChildren(QPushButton))
    assert not any("归档了" in t for t in _items(bar))
    next(b for b in bar.findChildren(QPushButton) if "已归档" in b.text()).click()
    assert any("归档了" in t for t in _items(bar))


def test_sidebar_group_header_tooltip_states_boundary(qapp):
    """每个已登记工作区都出分组头（哪怕 0 个会话）—— 空工作区也必须能在侧栏被操作。"""
    bar = Sidebar()
    bar.update_workspaces([_info(id="ws_1", name="我的项目")], "ws_1", [])
    tooltip = _head(bar, "我的项目").toolTip()
    assert "安全边界" in tooltip
    assert "不随数据根一起迁移" in tooltip


# -- rev58：新建入口（组头 ＋ / ▾ 菜单） --------------------------------------
def test_sidebar_group_add_button_emits_new_session_in(qapp):
    """组头行尾的小 ＋：一键在该工作区新建（rev58 易用性入口）。"""
    bar = Sidebar()
    bar.update_workspaces(
        [_info(id=WS_DEFAULT, name="默认工作区", builtin=True), _info(id="ws_1", name="我的项目")],
        WS_DEFAULT,
        [],
    )
    seen: list[str] = []
    bar.new_session_in.connect(seen.append)
    buttons = [
        b
        for b in bar.findChildren(QPushButton)
        if b.objectName() == "wsGroupAdd" and "我的项目" in b.toolTip()
    ]
    assert len(buttons) == 1  # 每个已登记工作区恰好一个入口
    buttons[0].click()
    assert seen == ["ws_1"]


def test_sidebar_new_menu_lists_every_workspace(qapp):
    """「＋ 新对话 ▾」菜单动态列出全部工作区；点选即 new_session_in（不弹 real menu）。"""
    bar = Sidebar()
    bar.update_workspaces(
        [_info(id=WS_DEFAULT, name="默认工作区", builtin=True), _info(id="ws_1", name="我的项目")],
        "ws_1",
        [],
    )
    bar._fill_new_menu()
    texts = [a.text() for a in bar._new_menu.actions()]
    assert any("当前工作区（我的项目）" in t for t in texts)
    assert any("在「默认工作区」新建" in t for t in texts)
    target = next(a for a in bar._new_menu.actions() if "默认工作区" in a.text())
    seen: list[str] = []
    bar.new_session_in.connect(seen.append)
    target.trigger()
    assert seen == [WS_DEFAULT]


def test_chat_header_new_menu_emits_new_session_in(qapp):
    """聊天页「新建会话 ▾」与侧栏同款：选工作区 → new_session_in（窗口层转 NewSession 请求）。"""
    from gui.chat.header import ChatHeader

    header = ChatHeader()
    header.set_workspaces(
        [_info(id=WS_DEFAULT, name="默认工作区", builtin=True), _info(id="ws_1", name="我的项目")],
        WS_DEFAULT,
    )
    header._fill_new_menu()
    texts = [a.text() for a in header._new_menu.actions()]
    assert any("当前工作区（默认工作区）" in t for t in texts)
    assert any("在「我的项目」新建" in t for t in texts)
    seen: list[str] = []
    header.new_session_in.connect(seen.append)
    next(a for a in header._new_menu.actions() if "我的项目" in a.text()).trigger()
    assert seen == ["ws_1"]


# -- 窗口级 -------------------------------------------------------------------
def _window(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda _store: MockGateway())
    return ctx, MainWindow(ctx.bridge, data_root=str(ctx.root))


def test_window_shows_default_workspace_group(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        qapp.processEvents()
        assert "默认工作区" in _head(window.sidebar, "默认工作区").text()
    finally:
        ctx.worker.stop()


def test_window_follows_create_and_collapse(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        ext = tmp_path / "proj"
        ext.mkdir()
        ctx.controller.handle(WorkspaceCreate(name="我的项目", root_kind="external", root=str(ext)))
        qapp.processEvents()

        assert any("我的项目" in t for t in _labels(window.workspaces_page))
        workspace_id = ctx.controller.workspace_manager.current()

        ctx.controller.handle(NewSession(title="项目里的会话"))
        qapp.processEvents()
        assert any("项目里的会话" in t for t in _items(window.sidebar))

        window.sidebar.collapse_workspace.emit(workspace_id, True)
        qapp.processEvents()
        assert not any("项目里的会话" in t for t in _items(window.sidebar))
        assert window._collapsed_cache == [workspace_id]
    finally:
        ctx.worker.stop()


def test_window_chat_new_menu_routes_to_workspace(qapp, tmp_path, monkeypatch):
    """rev58 接线：聊天页 ▾ 菜单选工作区 → NewSession(workspace_id=…) 真正到达控制器。"""
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        ext = tmp_path / "proj"
        ext.mkdir()
        ctx.controller.handle(
            WorkspaceCreate(name="我的项目", root_kind="external", root=str(ext))
        )
        qapp.processEvents()
        workspace_id = ctx.controller.workspace_manager.current()
        assert workspace_id != WS_DEFAULT

        window.chat.new_session_in.emit(workspace_id)
        # 信号经工作线程排队处理，耗时不可假设 —— 轮询等到落库再断言
        metas = []
        for _ in range(50):
            QTest.qWait(100)
            metas = ctx.controller.store.list(include_archived=True)
            if any(getattr(m, "workspace_id", None) == workspace_id for m in metas):
                break
        assert any(getattr(m, "workspace_id", None) == workspace_id for m in metas)
    finally:
        ctx.worker.stop()


def test_window_rail_opens_workspace_page(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.sidebar.open_workspaces.emit()
        qapp.processEvents()
        assert window.stack.currentWidget() is window.workspaces_page
    finally:
        ctx.worker.stop()


def test_window_detail_event_fills_file_list(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        ext = tmp_path / "proj"
        (ext / "src").mkdir(parents=True)
        (ext / "src" / "main.py").write_text("print(1)", encoding="utf-8")
        ctx.controller.handle(WorkspaceCreate(name="项目", root_kind="external", root=str(ext)))
        qapp.processEvents()
        workspace_id = ctx.controller.workspace_manager.current()

        ctx.controller.handle(WorkspaceDetail(id=workspace_id))
        qapp.processEvents()

        assert any("main.py" in t for t in _items(window.workspaces_page))
    finally:
        ctx.worker.stop()


def test_window_remove_workspace_updates_both_faces(tmp_path, monkeypatch, qapp):
    """移除后：侧栏分组消失、工作区页卡片消失（两处都由同一份快照重建）。"""
    from shared.envelope import WorkspaceDelete

    ctx, window = _window(tmp_path, monkeypatch)
    try:
        ctx.controller.push_initial_state()
        ext = tmp_path / "proj"
        ext.mkdir()
        ctx.controller.handle(WorkspaceCreate(name="临时项目", root_kind="external", root=str(ext)))
        qapp.processEvents()
        workspace_id = ctx.controller.workspace_manager.current()

        ctx.controller.handle(WorkspaceDelete(id=workspace_id))
        qapp.processEvents()

        assert not any("临时项目" in t for t in _labels(window.workspaces_page))
        assert ext.is_dir()  # R2：磁盘文件仍在
        assert any("默认工作区" in t for t in _labels(window.workspaces_page))
    finally:
        ctx.worker.stop()
