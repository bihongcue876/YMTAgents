"""主窗体：侧栏 + 主区（QStackedWidget），事件分发（spec §3.1 / rev2 §1.1）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
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
    SummarizeSession,
    RevertSession,
    SwitchBranch,
    SetSlot,
    SettingsUpdate,
    SwitchModel,
    TestConnection,
    UnarchiveSession,
)

from core.bus.bridge import BusBridge

from gui import theme
from gui.chat.session_panel import SessionPanel
from gui.chat.view import ChatView
from gui.pages.models import ModelsPage
from gui.pages.personas import PersonasPage
from gui.pages.settings import SettingsPage
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
        self._active_branch: str = "br0"  # rev31：当前活动分支（摘要文件按分支定位）
        self._session_titles: dict[str, str] = {}
        # rev14/rev23：模型与角色下拉都显示**当前会话**的选择；
        # 无会话/未选时回落全局默认（模型=slots.main，角色=manifest.current）
        self._session_models: dict[str, str | None] = {}
        self._session_personas: dict[str, str | None] = {}
        self._providers_cache: list = []
        self._slots_cache: dict = {}
        self._personas_cache: list = []
        self._default_persona: str | None = None

        self.sidebar = Sidebar()
        self.chat = ChatView()
        self.models = ModelsPage()
        self.personas_page = PersonasPage()
        self.settings = SettingsPage(data_root)
        self._theme: str | None = None
        self._font_size: str | None = None
        # 首帧即带外观，避免持久化暗色/大字号启动时的默认外观闪现
        self._apply_appearance(theme.DEFAULT_THEME, theme.DEFAULT_FONT_SIZE)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.chat)
        self.stack.addWidget(self.models)
        self.stack.addWidget(self.personas_page)
        self.stack.addWidget(self.settings)

        # 右侧会话详情面板（rev24）：默认收起，随 chat 头条「详情」或侧栏右键唤起
        self.detail = SessionPanel()
        self.detail.setVisible(False)
        self._detail_w = 380  # rev29：默认宽一档，避免右栏按钮文字被挤
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
        s.open_settings.connect(lambda: self.stack.setCurrentWidget(self.settings))

        c = self.chat
        c.new_session.connect(lambda: self.bus.submit(NewSession()))
        c.send_message.connect(lambda text: self.bus.submit(SendMessage(text=text)))
        c.cancel_turn.connect(lambda: self.bus.submit(CancelTurn()))
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

        self.settings.settings_update.connect(
            lambda section, data: self.bus.submit(SettingsUpdate(section=section, data=data))
        )

        self.detail.close_requested.connect(self._toggle_detail)
        self.detail.save_requested.connect(self._on_session_save)
        self.detail.question_selected.connect(self.chat.messages.scroll_to_user)
        self.detail.summarize_requested.connect(self._on_summarize)
        self.detail.open_summary_requested.connect(self._open_summary)
        self.detail.revert_requested.connect(self._on_revert)
        self.detail.branch_requested.connect(self._on_branch)
        self.detail.branch_switch_requested.connect(self._on_switch_branch)

    # -- 会话详情（rev24） --------------------------------------------------
    def _toggle_detail(self) -> None:
        visible = not self.detail.isVisible()
        self.detail.setVisible(visible)
        total = max(self.chat_split.width() - self.chat_split.handleWidth(), 100)
        if visible:
            # rev29：默认/最小再加宽一档 —— 原 280–520 会挤掉右栏按钮文字。
            width = min(max(self._detail_w, 340), 560)
            self.chat_split.setSizes([total - width, width])
            if self._current_session_id:
                self.bus.submit(SessionDetail(session_id=self._current_session_id))
        else:
            self._detail_w = max(self.detail.width(), 340)
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
                summary_threshold=data.get("summary_threshold"),
            )
        )

    def _on_summarize(self) -> None:
        if self._current_session_id:
            self.bus.submit(SummarizeSession(session_id=self._current_session_id))

    def _open_summary(self) -> None:
        """用系统默认程序打开本会话的 summary.md（不写盘，仅查看/编辑用）。"""
        if not self._current_session_id:
            return
        session_dir = Path(self._data_root) / "sessions" / self._current_session_id
        path = session_dir / "branches" / self._active_branch / "summary.md"
        if not path.exists() and self._active_branch == "br0":
            path = session_dir / "summary.md"  # rev26 旧布局（br0）
        if not path.exists():  # rev27：文件不存在时给可读提示，避免系统静默失败
            QMessageBox.information(self, "尚未压缩", "本会话还没有摘要文件，请先点「压缩历史」。")
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
        self.models.set_theme(used)

    # -- 事件分发 ----------------------------------------------------------
    def on_event(self, event) -> None:
        t = event.type
        if t == "session.index":
            self._session_titles = {m.id: m.title for m in event.sessions}
            self._session_models = {m.id: m.main_model for m in event.sessions}
            self._session_personas = {m.id: m.persona_id for m in event.sessions}
            self.sidebar.update_sessions(event.sessions)
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
        elif t == "error":
            self.chat.on_error(event)
        elif t == "provider.list":
            self._providers_cache = list(event.providers)
            self._slots_cache = dict(event.slots or {})
            self.models.update_providers(event.providers, event.slots)
            self._sync_model_dropdown()
        elif t == "provider.test.result":
            self.models.on_test_result(event)
        elif t == "provider.models.result":
            self.models.on_models_result(event)
        elif t == "settings.state":
            ui = event.data.get("ui", {})
            self._apply_appearance(ui.get("theme"), ui.get("font_size"))
            self.settings.load_settings(event.data)
        elif t == "persona.list":
            self._personas_cache = list(event.personas)
            self._default_persona = next(
                (p.id for p in event.personas if p.is_default), None
            )
            self.personas_page.update_personas(event)
            self._sync_persona_dropdown()
        elif t == "session.summary.result":
            # 成功由随后的 session.detail.result 刷新状态；失败在此提示且不改动原状
            if not event.ok and event.error:
                self.detail.set_summary_error(event.error)
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
