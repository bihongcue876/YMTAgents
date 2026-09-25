"""主窗体：侧栏 + 主区（QStackedWidget），事件分发（spec §3.1 / rev2 §1.1）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QWidget,
)

from shared.envelope import (
    ArchiveSession,
    BranchSession,
    CancelTurn,
    DeleteSession,
    FetchModels,
    NewSession,
    PersonaDelete,
    PersonaExport,
    PersonaImport,
    PersonaSave,
    PersonaSetDefault,
    PersonaSwitch,
    ProviderDelete,
    ProviderUpsert,
    RenameSession,
    ResumeSession,
    SendMessage,
    SessionDetail,
    SessionParams,
    SessionUpdate,
    CompressMemory,
    RevertSession,
    SwitchBranch,
    SetSlot,
    SettingsUpdate,
    FeatureToggle,
    BtcmRun,
    BtcmUpdate,
    SwitchModel,
    TestConnection,
    UnarchiveSession,
    McpServerDelete,
    McpServerReconnect,
    McpServerToggle,
    BuiltinToolToggle,
    BuiltinToolState,
    McpServerUpsert,
    McpServersRefresh,
    McpScan,
    McpScanResult,
    SkillDelete,
    SkillImport,
    SkillPermission,
    SkillToggle,
    SkillUpdate,
    SkillsRefresh,
    GateRespond,
    ShellClose,
    ShellInput,
    ShellRefresh,
    ShellSpawn,
    MoveSession,
    WorkspaceCreate,
    WorkspaceDelete,
    WorkspaceDetail,
    WorkspaceRefresh,
    WorkspaceSwitch,
    WorkspaceUpdate,
WorkspaceMemoryWrite,
    WorkspaceBuild,
    LibraryCreate,
    LibraryUpdate,
    LibraryDelete,
    LibrarySwitch,
    LibraryRefresh,
    LibraryDetail,
    LibraryIngest,
    LibraryQuery,
)

from shared.ids import WS_DEFAULT

from core.bus.bridge import BusBridge

from gui import theme
from gui.chat.session_panel import SessionPanel
from gui.chat.view import ChatView
from gui.pages.models import ModelsPage
from gui.pages.library import LibraryPage
from gui.pages.personas import PersonasPage
from gui.pages.plugins import PluginsPage
from gui.pages.settings import SettingsPage
from gui.pages.skills import SkillsPage
from gui.pages.terminal import TerminalPage
from gui.pages.thinking import ThinkingPage
from gui.pages.workspaces import WorkspacesPage
from gui.sidebar import PANEL_MAX_PX, PANEL_MIN_PX, RAIL_PX, Sidebar


class MainWindow(QMainWindow):
    def __init__(self, bus: BusBridge, data_root: str = "") -> None:
        super().__init__()
        self.bus = bus
        self._data_root = data_root
        self.setWindowTitle("言明通 / YMTAgents")
        self.resize(*self._default_size())

        self._current_session_id: str | None = None
        self._current_events: list[dict] = []  # rev24：问题列表来源
        self._active_branch: str = "br0"  # rev31：当前活动分支（记忆文件按分支定位）
        self._session_titles: dict[str, str] = {}
        # rev14/rev23：模型与角色下拉都显示**当前会话**的选择；
        # 无会话/未选时回落全局默认（模型=slots.main，角色=manifest.current）
        self._session_models: dict[str, str | None] = {}
        self._session_personas: dict[str, str | None] = {}
        self._providers_cache: list = []
        self._slots_cache: dict = {}
        self._personas_cache: list = []
        self._default_persona: str | None = None
        # v0.0.6：工作区缓存（分组由 Sidebar 负责，此处只留一份供重绘与查询）
        self._workspaces_cache: list = []
        self._current_workspace: str = WS_DEFAULT
        self._collapsed_cache: list[str] = []

        self.sidebar = Sidebar()
        self.chat = ChatView()
        self.models = ModelsPage()
        self.personas_page = PersonasPage()
        self.plugins_page = PluginsPage()
        self.skills_page = SkillsPage()
        self.skills_page.set_data_root(data_root)
        self.terminal_page = TerminalPage()
        self.thinking_page = ThinkingPage()
        self.library_page = LibraryPage()
        self.workspaces_page = WorkspacesPage()
        self.settings = SettingsPage(data_root)
        self._theme: str | None = None
        self._font_size: str | None = None
        # 首帧即带外观，避免持久化暗色/大字号启动时的默认外观闪现
        self._apply_appearance(theme.DEFAULT_THEME, theme.DEFAULT_FONT_SIZE)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.chat)
        self.stack.addWidget(self.models)
        self.stack.addWidget(self.personas_page)
        self.stack.addWidget(self.plugins_page)
        self.stack.addWidget(self.skills_page)
        self.stack.addWidget(self.terminal_page)
        self.stack.addWidget(self.thinking_page)
        self.stack.addWidget(self.library_page)
        self.stack.addWidget(self.workspaces_page)
        self.stack.addWidget(self.settings)
        # rail 导航态随页切换校准（rev55）：索引与上面的添加顺序一致
        self._page_keys: list[str | None] = [
            None, "models", "personas", "plugins", "skills", "terminal", "thinking", "library",
            "workspaces", "settings",
        ]
        self.stack.currentChanged.connect(self._on_page_changed)

        # 右侧会话详情面板（rev24）：默认收起，随 chat 头条「详情」或侧栏右键唤起
        self.detail = SessionPanel()
        self.detail.setVisible(False)
        self._detail_w = 500  # rev36：默认再加宽一档，按钮与长文案更舒展
        self.chat_split = QSplitter(Qt.Horizontal)
        self.chat_split.setChildrenCollapsible(False)
        self.chat_split.addWidget(self.stack)
        self.chat_split.addWidget(self.detail)
        self.chat_split.setStretchFactor(0, 1)
        self.chat_split.setStretchFactor(1, 0)
        self.chat_split.setSizes([836, 0])

        # 侧栏可拖拽调宽（rev13）：QSplitter 取代固定 264px；折叠逻辑见 _toggle_sidebar
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.chat_split)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([312, 836])
        self._last_sidebar_w = 312

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)
        self.setCentralWidget(central)

        self.sidebar.toggle_requested.connect(self._toggle_sidebar)
        self._connect_signals()
        bus.event_received.connect(self.on_event)

    @staticmethod
    def _default_size() -> tuple[int, int]:
        """启动尺寸（rev18）：按可用桌面面积的 ~86% 取，上下限兜底。

        原先固定 1100×720 —— 在高分屏上开局就显小、四周空一大圈。
        上限 1440×920 防止超宽屏上拉出过长行宽；下限 1024×720 保证三视图可用。
        """
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return 1024, 720
        avail = screen.availableGeometry()
        w = min(1440, max(1024, int(avail.width() * 0.86)))
        h = min(920, max(720, int(avail.height() * 0.86)))
        return w, h

    def _sync_model_dropdown(self) -> None:
        """模型下拉同步（rev14/rev23）：显示**当前会话**的选择；无会话/未选时回落全局默认。"""
        current = self._session_models.get(self._current_session_id or "")
        self.chat.set_models(self._providers_cache, current or self._slots_cache.get("main"))

    def _sync_persona_dropdown(self) -> None:
        """角色下拉同步（rev23）：显示当前会话的角色；无会话/未选时回落全局默认角色。"""
        current = self._session_personas.get(self._current_session_id or "")
        self.chat.set_personas(self._personas_cache, current or self._default_persona)

    def _toggle_sidebar(self) -> None:
        """折叠：面板藏起、侧栏收成 rail 图标条；展开：回到上次拖拽宽度。"""
        if self.sidebar.panel_visible():
            self._last_sidebar_w = max(self.sidebar.width(), RAIL_PX + PANEL_MIN_PX)
            self.sidebar.set_panel_visible(False)
            total = max(self.splitter.width() - self.splitter.handleWidth(), 100)
            self.splitter.setSizes([RAIL_PX, total - RAIL_PX])
        else:
            self.sidebar.set_panel_visible(True)
            total = max(self.splitter.width() - self.splitter.handleWidth(), 100)
            target = min(max(self._last_sidebar_w, RAIL_PX + PANEL_MIN_PX), RAIL_PX + PANEL_MAX_PX)
            self.splitter.setSizes([target, total - target])

    # -- 信号 --------------------------------------------------------------
    def _connect_signals(self) -> None:
        s = self.sidebar
        s.new_session.connect(lambda: self.bus.submit(NewSession()))
        s.resume_session.connect(lambda sid: self.bus.submit(ResumeSession(session_id=sid)))
        s.archive_session.connect(lambda sid: self.bus.submit(ArchiveSession(session_id=sid)))
        s.unarchive_session.connect(lambda sid: self.bus.submit(UnarchiveSession(session_id=sid)))
        s.delete_session.connect(lambda sid: self.bus.submit(DeleteSession(session_id=sid)))
        s.detail_session.connect(self._open_detail)
        s.open_models.connect(lambda: self.stack.setCurrentWidget(self.models))
        s.open_personas.connect(lambda: self.stack.setCurrentWidget(self.personas_page))
        s.open_skills.connect(lambda: self.stack.setCurrentWidget(self.skills_page))
        s.open_plugins.connect(lambda: self.stack.setCurrentWidget(self.plugins_page))
        s.open_terminal.connect(lambda: self.stack.setCurrentWidget(self.terminal_page))
        s.open_thinking.connect(lambda: self.stack.setCurrentWidget(self.thinking_page))
        s.open_library.connect(lambda: self.stack.setCurrentWidget(self.library_page))
        s.open_workspaces.connect(lambda: self.stack.setCurrentWidget(self.workspaces_page))
        s.open_settings.connect(lambda: self.stack.setCurrentWidget(self.settings))

        c = self.chat
        c.new_session.connect(lambda: self.bus.submit(NewSession()))
        c.new_session_in.connect(lambda wid: self.bus.submit(NewSession(workspace_id=wid)))  # rev58
        c.send_message.connect(
            lambda text, attachments: self.bus.submit(
                SendMessage(text=text, attachments=list(attachments or []))
            )
        )
        c.command_run.connect(self._on_command)
        c.command_error.connect(lambda message: QMessageBox.information(self, "命令", message))
        c.cancel_turn.connect(lambda: self.bus.submit(CancelTurn()))
        # v0.0.11：关卡卡片在对话内裁决，直接回发（core 侧仍在泵取队列等待）
        c.gates.decided.connect(
            lambda call_id, allow: self.bus.submit(
                GateRespond(call_id=call_id, decision="allow" if allow else "deny")
            )
        )
        c.switch_model.connect(lambda mid: self.bus.submit(SwitchModel(slot="main", model_id=mid)))
        c.switch_persona.connect(
            lambda pid: self.bus.submit(PersonaSwitch(persona_id=pid))
        )
        c.rename_session.connect(self._on_rename)
        c.toggle_detail.connect(self._toggle_detail)
        # 空状态 CTA（rev9 §6）：无供应商时一键跳模型配置页
        c.add_model.connect(lambda: self.stack.setCurrentWidget(self.models))

        m = self.models
        m.upsert_requested.connect(
            lambda spec, key: self.bus.submit(ProviderUpsert(provider=spec, api_key=key))
        )
        m.delete_requested.connect(lambda pid: self.bus.submit(ProviderDelete(provider_id=pid)))
        m.test_requested.connect(
            lambda pid, mid: self.bus.submit(TestConnection(provider_id=pid, model_id=mid))
        )
        # 模型导入（rev9 §2）：拉取端点自报的模型列表，用户勾选后走既有 upsert 落盘
        m.models_requested.connect(lambda pid: self.bus.submit(FetchModels(provider_id=pid)))
        # 模型配置页·槽位绑定区 → 全局槽位（models.json 的 slots，spec rev4）
        m.slot_requested.connect(
            lambda slot, mid: self.bus.submit(SetSlot(slot=slot, model_id=mid or None))
        )

        p = self.personas_page
        p.save_requested.connect(
            lambda pid, name, prompt: self.bus.submit(
                PersonaSave(persona_id=pid, name=name, prompt=prompt)
            )
        )
        p.delete_requested.connect(lambda pid: self.bus.submit(PersonaDelete(persona_id=pid)))
        p.set_default_requested.connect(
            lambda pid: self.bus.submit(PersonaSetDefault(persona_id=pid))
        )
        p.export_requested.connect(
            lambda pid, path: self.bus.submit(PersonaExport(persona_id=pid, path=path))
        )
        p.import_requested.connect(lambda path: self.bus.submit(PersonaImport(path=path)))

        pl = self.plugins_page
        pl.upsert_requested.connect(lambda server: self.bus.submit(McpServerUpsert(server=server)))
        pl.delete_requested.connect(lambda sid: self.bus.submit(McpServerDelete(id=sid)))
        pl.toggle_requested.connect(
            lambda sid, enabled: self.bus.submit(McpServerToggle(id=sid, enabled=enabled))
        )
        pl.reconnect_requested.connect(lambda sid: self.bus.submit(McpServerReconnect(id=sid)))
        pl.refresh_requested.connect(lambda: self.bus.submit(McpServersRefresh()))
        pl.scan_requested.connect(lambda sid: self.bus.submit(McpScan(server_id=sid)))
        # v0.0.11（D-1）：内置工具开关（真值 plugins.json -> builtin；关 = 真卸载）
        pl.builtin_toggle_requested.connect(
            lambda name, enabled: self.bus.submit(BuiltinToolToggle(name=name, enabled=enabled))
        )

        sk = self.skills_page
        sk.toggle_requested.connect(
            lambda sid, enabled: self.bus.submit(SkillToggle(id=sid, enabled=enabled))
        )
        sk.import_requested.connect(lambda source: self.bus.submit(SkillImport(source=source)))
        sk.update_requested.connect(lambda sid: self.bus.submit(SkillUpdate(id=sid)))
        sk.delete_requested.connect(lambda sid: self.bus.submit(SkillDelete(id=sid)))
        sk.permission_requested.connect(
            lambda sid, perm: self.bus.submit(SkillPermission(id=sid, permission=perm))
        )
        sk.refresh_requested.connect(lambda: self.bus.submit(SkillsRefresh()))

        tm = self.terminal_page
        tm.spawn_requested.connect(lambda: self.bus.submit(ShellSpawn()))
        tm.close_requested.connect(lambda sid: self.bus.submit(ShellClose(id=sid)))
        tm.input_requested.connect(
            lambda sid, cmd: self.bus.submit(ShellInput(id=sid, command=cmd))
        )
        tm.refresh_requested.connect(lambda: self.bus.submit(ShellRefresh()))

        th = self.thinking_page
        th.update_requested.connect(
            lambda trigger, slot: self.bus.submit(BtcmUpdate(trigger=trigger, slot=slot))
        )
        th.run_requested.connect(
            lambda question, effort, mode: self.bus.submit(
                BtcmRun(question=question, effort=effort, mode=mode)
            )
        )

        # v0.0.6：工作区（侧栏分组 → 请求；页面 → 请求）
        s.switch_workspace.connect(lambda wid: self.bus.submit(WorkspaceSwitch(id=wid)))
        s.collapse_workspace.connect(self._on_collapse_workspace)
        s.new_session_in.connect(lambda wid: self.bus.submit(NewSession(workspace_id=wid)))
        s.rename_workspace.connect(self._on_rename_workspace)
        s.open_workspace_dir.connect(self._open_path)
        s.delete_workspace.connect(lambda wid: self.bus.submit(WorkspaceDelete(id=wid)))
        s.move_session.connect(
            lambda sid, wid: self.bus.submit(MoveSession(session_id=sid, workspace_id=wid or None))
        )
        wp = self.workspaces_page
        wp.create_requested.connect(
            lambda data: self.bus.submit(
                WorkspaceCreate(
                    name=data["name"],
                    root_kind=data["root_kind"],
                    root=data["root"],
                    data_home_kind=data["data_home_kind"],
                    build_cmd=data.get("build_cmd") or "",
                    note=data["note"],
                )
            )
        )
        wp.update_requested.connect(
            lambda data: self.bus.submit(
                WorkspaceUpdate(
                    id=data["id"],
                    name=data["name"],
                    note=data.get("note") or "",
                    build_cmd=data.get("build_cmd") or "",
                )
            )
        )
        wp.delete_requested.connect(lambda wid: self.bus.submit(WorkspaceDelete(id=wid)))
        wp.switch_requested.connect(lambda wid: self.bus.submit(WorkspaceSwitch(id=wid)))
        wp.refresh_requested.connect(lambda: self.bus.submit(WorkspaceRefresh()))
        wp.detail_requested.connect(
            lambda wid, path: self.bus.submit(WorkspaceDetail(id=wid, path=path))
        )
        wp.open_dir_requested.connect(self._open_path)
        wp.memory_write_requested.connect(
            lambda scope, workspace_id, mode, text: self.bus.submit(
                WorkspaceMemoryWrite(
                    scope=scope, workspace_id=workspace_id, mode=mode, text=text
                )
            )
        )
        wp.build_requested.connect(lambda wid: self.bus.submit(WorkspaceBuild(id=wid)))

        lp = self.library_page
        lp.create_requested.connect(
            lambda data: self.bus.submit(
                LibraryCreate(
                    name=data["name"], root_kind=data["root_kind"], root=data["root"],
                    group=data["group"], model_ref=data["model_ref"], note=data["note"],
                )
            )
        )
        lp.update_requested.connect(
            lambda data: self.bus.submit(
                LibraryUpdate(
                    id=data["id"], name=data.get("name"), root=data.get("root"),
                    group=data.get("group"), model_ref=data.get("model_ref"), note=data.get("note"),
                )
            )
        )
        lp.delete_requested.connect(lambda lid: self.bus.submit(LibraryDelete(id=lid)))
        lp.switch_requested.connect(lambda lid: self.bus.submit(LibrarySwitch(id=lid)))
        lp.refresh_requested.connect(lambda lid: self.bus.submit(LibraryRefresh(id=lid)))
        lp.detail_requested.connect(
            lambda lid, offset, event_limit, graph_limit, graph_ids, focus_event_id: self.bus.submit(
                LibraryDetail(
                    id=lid, event_offset=offset, event_limit=event_limit,
                    graph_limit=graph_limit, graph_library_ids=graph_ids,
                    focus_event_id=focus_event_id,
                )
            )
        )
        lp.ingest_requested.connect(
            lambda lid, text, event_type, index, event_id: self.bus.submit(
                LibraryIngest(
                    id=lid, text=text, event_type=event_type, index=index, event_id=event_id,
                )
            )
        )
        lp.query_requested.connect(
            lambda ids, query, mode, top_k: self.bus.submit(
                LibraryQuery(lib_ids=ids, query=query, mode=mode, top_k=top_k)
            )
        )

        self.settings.settings_update.connect(
            lambda section, data: self.bus.submit(SettingsUpdate(section=section, data=data))
        )
        self.settings.feature_toggle.connect(
            self._on_feature_toggle
        )

        self.detail.close_requested.connect(self._toggle_detail)
        self.detail.save_requested.connect(self._on_session_save)
        self.detail.question_selected.connect(self.chat.messages.scroll_to_user)
        self.detail.compress_requested.connect(self._on_compress)
        self.detail.open_memory_requested.connect(self._open_memory)
        self.detail.open_memory_history_requested.connect(self._open_memory_history)
        self.detail.revert_requested.connect(self._on_revert)
        self.detail.branch_requested.connect(self._on_branch)
        self.detail.branch_switch_requested.connect(self._on_switch_branch)

    # -- 会话详情（rev24） --------------------------------------------------
    def _on_feature_toggle(self, name: str, enabled: bool) -> None:
        """关闭 DPIM 时先清掉 GUI 缓存，再请求核心真卸载。"""
        if name == "dpim" and not enabled:
            self.library_page.set_available(False)
        self.bus.submit(FeatureToggle(name=name, enabled=enabled))

    def _on_command(self, name: str, action: str, argument: str) -> None:
        """命令系统的**唯一**执行点：把命令映射为既有按钮动作（不新开任何通道）。

        `navigate` → 切页；`new_session` → 发 `NewSession`（可按工作区名定位）；
        `compress` → 发 `CompressMemory(force=True)`；`detail` → 右栏开关；
        `theme` → `settings.update(section="ui")`；`clear_view` → 仅清界面；`help` → 展示命令表。
        """
        from gui.chat import commands

        if action == "help":
            QMessageBox.information(self, "可用命令", commands.help_text())
            return
        if action == "navigate":
            command = commands.by_name(name)
            pages = {
                "models": self.models,
                "personas": self.personas_page,
                "plugins": self.plugins_page,
                "skills": self.skills_page,
                "terminal": self.terminal_page,
                "thinking": self.thinking_page,
                "library": self.library_page,
                "workspaces": self.workspaces_page,
                "settings": self.settings,
            }
            widget = pages.get(command.target if command is not None else "")
            if widget is not None:
                self.stack.setCurrentWidget(widget)
            return
        if action == "new_session":
            workspace_id = None
            label = argument.strip()
            if label:
                match = next(
                    (w for w in self._workspaces_cache
                     if str(w.get("name") or "").strip().casefold() == label.casefold()),
                    None,
                )
                if match is None:
                    QMessageBox.information(self, "命令", f"未找到工作区：{label}")
                    return
                workspace_id = None if match.get("builtin") else match.get("id")
            self.bus.submit(NewSession(workspace_id=workspace_id))
            return
        if action == "compress":
            if self._current_session_id:
                self.bus.submit(CompressMemory(force=True))
            return
        if action == "detail":
            self._toggle_detail()
            return
        if action == "clear_view":
            self.chat.clear()
            return
        if action == "theme":
            theme_name = argument.strip().lower()
            if theme_name not in ("light", "dark"):
                QMessageBox.information(self, "命令", "用法：/theme light 或 /theme dark")
                return
            self.bus.submit(SettingsUpdate(section="ui", data={"theme": theme_name}))

    def _toggle_detail(self) -> None:
        visible = not self.detail.isVisible()
        self.detail.setVisible(visible)
        total = max(self.chat_split.width() - self.chat_split.handleWidth(), 100)
        if visible:
            # rev36：默认再加宽一档（440 → 500），上限放到 720。
            width = min(max(self._detail_w, 380), 720)
            self.chat_split.setSizes([total - width, width])
            if self._current_session_id:
                self.bus.submit(SessionDetail(session_id=self._current_session_id))
        else:
            self._detail_w = max(self.detail.width(), 380)
            self.chat_split.setSizes([total, 0])
        self.chat.set_detail_active(visible)

    def _open_detail(self, session_id: str) -> None:
        """侧栏右键「详情」：必要时先切到该会话，再展开面板。"""
        if session_id != self._current_session_id:
            self.bus.submit(ResumeSession(session_id=session_id))
        if not self.detail.isVisible():
            self._toggle_detail()
        else:
            self.bus.submit(SessionDetail(session_id=session_id))

    def _on_session_save(self, data: dict) -> None:
        if not self._current_session_id:
            return
        self.bus.submit(
            SessionUpdate(
                session_id=self._current_session_id,
                title=data.get("title", ""),
                note=data.get("note", ""),
                max_context=data.get("max_context"),
                params=SessionParams(**data.get("params", {})),
                memory_use=data.get("memory_use"),
                memory_compress=data.get("memory_compress"),
                memory_auto=data.get("memory_auto"),
                memory_threshold=data.get("memory_threshold"),
            )
        )

    def _on_compress(self) -> None:
        if self._current_session_id:
            # 用户显式点按 = 强求压缩（忽略「是否压缩」开关，v0.0.1）
            self.bus.submit(CompressMemory(session_id=self._current_session_id, force=True))

    def _open_memory(self) -> None:
        """用系统默认程序打开本会话的 memory.md（不写盘，仅查看/编辑用）。"""
        if not self._current_session_id:
            return
        session_dir = Path(self._data_root) / "sessions" / self._current_session_id
        path = session_dir / "branches" / self._active_branch / "memory.md"
        if not path.exists() and self._active_branch == "br0":
            path = session_dir / "summary.md"  # v0.0.1 之前旧布局（br0）
        if not path.exists():  # 文件不存在时给可读提示，避免系统静默失败
            QMessageBox.information(self, "尚未建立记忆", "本会话还没有记忆文件，请先点「压缩记忆」。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_memory_history(self, revision: int) -> None:
        """查看某版旧记忆（v0.0.1）：只读、不注入上下文。"""
        if not self._current_session_id:
            return
        session_dir = Path(self._data_root) / "sessions" / self._current_session_id
        path = (
            session_dir / "branches" / self._active_branch / "memory" / "history" / f"{revision}.md"
        )
        if not path.exists():
            QMessageBox.information(self, "旧记忆不存在", "该版本旧记忆文件已不存在。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _user_seq(self, ordinal: int) -> int | None:
        """第 ordinal 个用户提问（0 起）对应的事件 seq（rev31：分支/回退要用 seq）。"""
        seen = 0
        for event in self._current_events:
            if event.get("type") != "msg.user":
                continue
            if seen == ordinal:
                try:
                    return int(event.get("seq", -1))
                except (TypeError, ValueError):
                    return None
            seen += 1
        return None

    def _on_revert(self, ordinal: int) -> None:
        if not self._current_session_id:
            return
        seq = self._user_seq(ordinal)
        if seq is None:
            return
        self.bus.submit(RevertSession(session_id=self._current_session_id, to_seq=seq))

    def _on_branch(self, ordinal: int) -> None:
        if not self._current_session_id:
            return
        seq = self._user_seq(ordinal)
        if seq is None:
            return
        self.bus.submit(BranchSession(session_id=self._current_session_id, from_seq=seq))

    def _on_switch_branch(self, branch_id: str) -> None:
        if self._current_session_id:
            self.bus.submit(
                SwitchBranch(session_id=self._current_session_id, branch_id=branch_id)
            )

    def _questions(self) -> list[str]:
        """当前会话的用户提问（rev30：完整内容，仅把换行折成空格），供右栏问题列表跳转。"""
        result: list[str] = []
        for event in self._current_events:
            if event.get("type") != "msg.user":
                continue
            text = (event.get("payload") or {}).get("text", "")
            text = " ".join(text.split()) or "（空）"
            result.append(text)
        return result

    def _on_rename(self, title: str) -> None:
        if self._current_session_id:
            self.bus.submit(RenameSession(session_id=self._current_session_id, title=title))

    # -- 外观 --------------------------------------------------------------
    def _on_page_changed(self, index: int) -> None:
        """页切换 → rail 导航态（rev55）：回对话页复位全部按钮。"""
        key = self._page_keys[index] if 0 <= index < len(self._page_keys) else None
        self.sidebar.set_active(key)

    def _apply_appearance(self, name: str | None, font_size: str | None) -> None:
        """应用外观（主题 + 字号）：全局 QSS + 需自渲染的视图重绘。

        settings.state 在每次设置更新时都会发，故此处必须幂等且廉价；
        两项各自判变，改其一不牵连另一项重渲染。
        """
        used = theme.palette(name).name
        used_font = theme.font_level(font_size).name
        if used == self._theme and used_font == self._font_size:
            return
        self._theme = used
        self._font_size = used_font
        theme.apply(used, used_font)
        self.chat.set_theme(used, used_font)
        # 样式表字号不参与 sizeHint：居中/紧凑布局里的标题会被裁，故重算最小宽高（rev11/rev16）
        self.chat.empty.refresh_metrics(used_font)
        self.models.refresh_metrics(used_font)
        self.settings.refresh_metrics(used_font)
        self.thinking_page.refresh_metrics(used_font)
        self.library_page.set_theme(used)
        self.models.set_theme(used)
        # 终端监视区是自渲染内容 → 整帧重渲染（颜色/字号经 theme 注入，模板不写死）
        self.terminal_page.set_theme(used, used_font)
        # 侧栏分组与工作区页把行高渲染成固定值 → 字号变了必须重算（样式表字号不参与 sizeHint）
        self.sidebar.rebuild()
        self.workspaces_page.refresh_metrics()

    # -- 事件分发 ----------------------------------------------------------
    def on_event(self, event) -> None:
        t = event.type
        if t == "session.index":
            self._session_titles = {m.id: m.title for m in event.sessions}
            self._session_models = {m.id: m.main_model for m in event.sessions}
            self._session_personas = {m.id: m.persona_id for m in event.sessions}
            self.sidebar.update_sessions(event.sessions)
            self.terminal_page.set_session_titles(self._session_titles)
            self._sync_model_dropdown()
            self._sync_persona_dropdown()
            if self._current_session_id in self._session_titles:
                self.chat.set_title(self._session_titles[self._current_session_id])
        elif t == "session.created":
            self._current_session_id = event.session_id
            self._current_events = []
            self.detail.clear()
            self.chat.set_title(event.title)
            self.chat.clear()
            self.stack.setCurrentWidget(self.chat)
            self._sync_model_dropdown()
            self._sync_persona_dropdown()
        elif t == "session.events":
            self._current_session_id = event.session_id
            self._current_events = list(event.events)
            self.chat.load_session(self._session_titles.get(event.session_id, ""), event.events)
            self.stack.setCurrentWidget(self.chat)
            self._sync_model_dropdown()
            self._sync_persona_dropdown()
        elif t == "msg.assistant.delta":
            if event.reasoning:
                self.chat.on_reasoning(event)
            else:
                self.chat.on_delta(event)
        elif t == "msg.assistant.final":
            self.chat.on_final(event)
        elif t == "turn.status":
            self.chat.on_status(event)
        elif t == "tool.call":
            self.chat.on_tool_call(event)
        elif t == "tool.builtin.state":
            self.plugins_page.update_builtin(event.items)
        elif t == "tool.result":
            self.chat.on_tool_result(event)
            self._settle_gate(event)
            if str(getattr(event, "call_id", "")).startswith("btcm-page-"):
                output = event.output if event.ok else (
                    (event.error or {}).get("message", "") if event.error else ""
                )
                self.thinking_page.on_run_result(
                    bool(event.ok), output or "", event.usage, event.duration_ms
                )
        elif t == "btcm.state":
            self.thinking_page.set_state(event.trigger, event.slot, event.ready)
        elif t == "think.delta":
            self.thinking_page.on_delta(event.agent, event.kind, event.text)
        elif t == "think.iteration":
            self.thinking_page.on_iteration(event.iteration, event.verdict, event.decision)
        elif t == "gate.result":
            self._on_gate_result(event)
        elif t == "error":
            self.chat.on_error(event)
            if event.scope == "library":
                self.library_page.set_error(event.message)
        elif t == "provider.list":
            self._providers_cache = list(event.providers)
            self._slots_cache = dict(event.slots or {})
            self.models.update_providers(event.providers, event.slots)
            self.library_page.set_providers(event.providers)
            self._sync_model_dropdown()
        elif t == "provider.test.result":
            self.models.on_test_result(event)
        elif t == "provider.models.result":
            self.models.on_models_result(event)
        elif t == "feature.state":
            self.settings.set_features(event.features)
        elif t == "retrieval.state":
            self.settings.set_retrieval_state(event.engines, event.default_engines, event.module_state)
        elif t == "retrieval.test.result":
            self.settings.set_retrieval_test_result(event.engine, event.findings, event.summary)
            dpim = next((item for item in event.features if item.get("name") == "dpim"), {})
            self.library_page.set_available(
                bool(dpim.get("enabled")), str(dpim.get("state", "disabled"))
            )
        elif t == "settings.state":
            ui = event.data.get("ui", {})
            self._apply_appearance(ui.get("theme"), ui.get("font_size"))
            self.chat.set_copy_buttons(bool(ui.get("copy_buttons", True)))
            self.settings.load_settings(event.data)
            self.workspaces_page.set_default_managed_kind(
                (event.data.get("workspace") or {}).get("default_data_home_kind", "inline")
            )
        elif t == "persona.list":
            self._personas_cache = list(event.personas)
            self._default_persona = next(
                (p.id for p in event.personas if p.is_default), None
            )
            self.personas_page.update_personas(event)
            self._sync_persona_dropdown()
        elif t == "persona.export.result":
            if event.ok:
                QMessageBox.information(self, "导出完成", f"角色「{event.name}」已导出：\n{event.path}")
            else:
                QMessageBox.warning(self, "导出失败", event.error or "导出失败。")
        elif t == "persona.import.result":
            if event.ok:
                QMessageBox.information(self, "导入完成", f"已导入角色「{event.name}」。")
            else:
                QMessageBox.warning(self, "导入失败", event.error or "导入失败。")
        elif t == "session.memory.result":
            # 成功由随后的 session.detail.result 刷新状态；失败在此提示且不改动原状
            if not event.ok and event.error:
                self.detail.set_memory_error(event.error)
        elif t == "session.branches":
            self._active_branch = event.active
            self.detail.set_branches(event)
        elif t == "session.detail.result":
            self._current_events = self._current_events or []
            if event.active_branch:
                self._active_branch = event.active_branch
            self.detail.set_detail(event, self._questions())
        elif t == "ctx.usage":
            # rev24：关闭 stage-1 遗留缺口 —— 用量事件被消费；面板可见时刷新详情
            if self.detail.isVisible() and self._current_session_id:
                self.bus.submit(SessionDetail(session_id=self._current_session_id))
        elif t == "mcp.server.list":
            self.plugins_page.update_servers(event.servers)
        elif t == "tool.list":
            self.plugins_page.update_tools(event.tools)
        elif t == "mcp.server.status":
            self.plugins_page.update_status(event.id, event.state, event.tools, event.error)
        elif t == "mcp.scan.result":
            self.plugins_page.update_scan(
                event.server_id, event.server_name, event.findings, event.summary
            )
        elif t == "skill.list":
            self.skills_page.update_skills(event.skills)
        elif t == "skill.import.result":
            if event.ok:
                if event.updated:
                    QMessageBox.information(self, "更新完成", f"技能「{event.names[0] if event.names else ''}」已更新。")
                else:
                    names = "、".join(event.names)
                    QMessageBox.information(self, "导入完成", f"已导入技能：{names}（默认停用，请在列表中启用）。")
            else:
                title = "更新失败" if event.updated else "导入失败"
                QMessageBox.warning(self, title, event.error or "操作失败。")
        elif t == "gate.request":
            self._on_gate_request(event)
        elif t == "shell.list":
            self.terminal_page.update_shells(
                event.shells, event.max_shells, event.permission, event.allow_restricted
            )
        elif t == "shell.output":
            self.terminal_page.on_output(event.id, event.chunk)
        elif t == "workspace.list":
            # 信封里是 WorkspaceInfo 模型（强类型契约）；到视图层统一转成 dict ——
            # 侧栏与工作区页只需「读几个字段」，不必各自依赖 shared 的类型（也不该绑死）。
            items = [w.model_dump() for w in event.workspaces]
            self._workspaces_cache = items
            self._current_workspace = event.current
            self._collapsed_cache = list(event.collapsed)
            self.sidebar.update_workspaces(items, event.current, event.collapsed)
            self.workspaces_page.update_workspaces(items, event.current)
            self.chat.update_workspaces(items, event.current)  # rev58：header ▾ 新建菜单数据源
            self.terminal_page.set_workspaces(items)
        elif t == "workspace.detail.result":
            self.workspaces_page.on_detail(event)
        elif t == "workspace.memory.result":
            if event.ok:
                QMessageBox.information(
                    self, "记忆已写入", f"已写入工作区记忆（{event.chars} 字）。"
                )
            else:
                QMessageBox.warning(self, "记忆写入失败", event.error or "写入失败。")
        elif t == "workspace.build.result":
            if event.ok:
                QMessageBox.information(
                    self,
                    "构建完成",
                    f"退出码 {event.exit_code} · 用时 {event.duration_ms} ms\n日志：{event.log_path or '—'}",
                )
            else:
                QMessageBox.warning(
                    self,
                    "构建未成功",
                    f"退出码：{event.exit_code if event.exit_code is not None else '—'} · "
                    f"用时 {event.duration_ms} ms\n日志：{event.log_path or '—'}",
                )
        elif t == "library.list":
            self.library_page.on_list(event)
        elif t == "library.detail.result":
            self.library_page.on_detail(event)
        elif t == "library.graph.result":
            self.library_page.on_graph(event)
        elif t == "library.ingest.result":
            self.library_page.on_ingest_result(event)
        elif t == "library.query.result":
            self.library_page.on_query_result(event)

    # -- 工作区（v0.0.6） --------------------------------------------------
    def _on_collapse_workspace(self, workspace_id: str, collapsed: bool) -> None:
        """折叠工作区分组 = 改 UI 偏好 → 落 `settings.ui.collapsed_workspaces`。

        **不新增请求/事件类型**（沿既有 `settings.update(section="ui")`，与主题/字号同族）；
        就地更新本地折叠态并重绘，界面不必等落盘往返。
        """
        ids = [w for w in self._collapsed_cache if w != workspace_id]
        if collapsed:
            ids.append(workspace_id)
        self._collapsed_cache = ids
        self.sidebar.update_workspaces(self._workspaces_cache, self._current_workspace, ids)
        self.bus.submit(SettingsUpdate(section="ui", data={"collapsed_workspaces": ids}))

    def _on_rename_workspace(self, workspace_id: str) -> None:
        info = next((w for w in self._workspaces_cache if w.get("id") == workspace_id), None)
        if info is None:
            return
        name, ok = QInputDialog.getText(self, "重命名工作区", "名称：", text=info.get("name", ""))
        if not ok or not name.strip():
            return
        self.bus.submit(
            WorkspaceUpdate(
                id=workspace_id,
                name=name.strip(),
                note=info.get("note") or "",
                build_cmd=info.get("build_cmd") or "",
            )
        )

    def _open_path(self, path: str) -> None:
        """在系统文件管理器中打开目录（GUI 侧动作，不经 shell —— 不让便利功能变成命令执行面）。"""
        if path and Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            QMessageBox.information(self, "目录不存在", f"该目录不存在或不可访问：\n{path}")

    def _on_gate_request(self, event) -> None:
        """工具调用关卡：**在对话框内**出示确认卡片（非模态，不跳页、不阻塞界面）。

        裁决由卡片回发 GateRespond，core 侧仍在泵取队列等待；用户放行后的结果由
        tool.result 收尾（卡片就地塌缩为记录），策略拒绝 / 超时由 gate.result 收尾。
        """
        self.stack.setCurrentWidget(self.chat)
        self.chat.gates.show_gate(event)

    def _on_gate_result(self, event) -> None:
        """关卡结果：只收「非用户放行」的收尾（放行等 tool.result 给准确摘要）。"""
        if str(getattr(event, "decision", "deny")) == "allow":
            return
        decider = str(getattr(event, "decider", "") or "")
        reason = "已拒绝（策略）" if decider == "policy" else "已拒绝（超时或未裁决）"
        self.chat.gates.resolve(event.call_id, reason, ok=False)

    def _settle_gate(self, event) -> None:
        """工具结果回填关卡卡片：塌缩为一行摘要（如「已修改 x（+1 / -1）」）。"""
        summary = event.output if event.ok else (
            (event.error or {}).get("message", "") if event.error else ""
        )
        first = (str(summary or "").splitlines() or [""])[0].strip()
        self.chat.gates.resolve(
            event.call_id, first, ok=bool(event.ok)
        )
