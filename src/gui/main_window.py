"""主窗体：侧栏 + 主区（QStackedWidget），事件分发（spec §3.1 / rev2 §1.1）。"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QStackedWidget, QWidget

from shared.envelope import (
    ArchiveSession,
    CancelTurn,
    DeleteSession,
    NewSession,
    ProviderDelete,
    ProviderUpsert,
    RenameSession,
    ResumeSession,
    SendMessage,
    SetSlot,
    SettingsUpdate,
    SwitchModel,
    TestConnection,
    UnarchiveSession,
)

from core.bus.bridge import BusBridge

from gui import theme
from gui.chat.view import ChatView
from gui.pages.models import ModelsPage
from gui.pages.settings import SettingsPage
from gui.sidebar import Sidebar


class MainWindow(QMainWindow):
    def __init__(self, bus: BusBridge, data_root: str = "") -> None:
        super().__init__()
        self.bus = bus
        self.setWindowTitle("言明通 / YMTAgents")
        self.resize(1100, 720)

        self._current_session_id: str | None = None
        self._session_titles: dict[str, str] = {}

        self.sidebar = Sidebar()
        self.chat = ChatView()
        self.models = ModelsPage()
        self.settings = SettingsPage(data_root)
        self._theme: str | None = None
        self._font_size: str | None = None
        # 首帧即带外观，避免持久化暗色/大字号启动时的默认外观闪现
        self._apply_appearance(theme.DEFAULT_THEME, theme.DEFAULT_FONT_SIZE)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.chat)
        self.stack.addWidget(self.models)
        self.stack.addWidget(self.settings)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._connect_signals()
        bus.event_received.connect(self.on_event)

    # -- 信号 --------------------------------------------------------------
    def _connect_signals(self) -> None:
        s = self.sidebar
        s.new_session.connect(lambda: self.bus.submit(NewSession()))
        s.resume_session.connect(lambda sid: self.bus.submit(ResumeSession(session_id=sid)))
        s.archive_session.connect(lambda sid: self.bus.submit(ArchiveSession(session_id=sid)))
        s.unarchive_session.connect(lambda sid: self.bus.submit(UnarchiveSession(session_id=sid)))
        s.delete_session.connect(lambda sid: self.bus.submit(DeleteSession(session_id=sid)))
        s.open_models.connect(lambda: self.stack.setCurrentWidget(self.models))
        s.open_settings.connect(lambda: self.stack.setCurrentWidget(self.settings))

        c = self.chat
        c.new_session.connect(lambda: self.bus.submit(NewSession()))
        c.send_message.connect(lambda text: self.bus.submit(SendMessage(text=text)))
        c.cancel_turn.connect(lambda: self.bus.submit(CancelTurn()))
        c.switch_model.connect(lambda mid: self.bus.submit(SwitchModel(slot="main", model_id=mid)))
        c.rename_session.connect(self._on_rename)

        m = self.models
        m.upsert_requested.connect(
            lambda spec, key: self.bus.submit(ProviderUpsert(provider=spec, api_key=key))
        )
        m.delete_requested.connect(lambda pid: self.bus.submit(ProviderDelete(provider_id=pid)))
        m.test_requested.connect(
            lambda pid, mid: self.bus.submit(TestConnection(provider_id=pid, model_id=mid))
        )
        # 模型配置页·槽位绑定区 → 全局槽位（models.json 的 slots，spec rev4）
        m.slot_requested.connect(
            lambda slot, mid: self.bus.submit(SetSlot(slot=slot, model_id=mid or None))
        )

        self.settings.settings_update.connect(
            lambda section, data: self.bus.submit(SettingsUpdate(section=section, data=data))
        )

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
        self.models.set_theme(used)

    # -- 事件分发 ----------------------------------------------------------
    def on_event(self, event) -> None:
        t = event.type
        if t == "session.index":
            self._session_titles = {m.id: m.title for m in event.sessions}
            self.sidebar.update_sessions(event.sessions)
            if self._current_session_id in self._session_titles:
                self.chat.set_title(self._session_titles[self._current_session_id])
        elif t == "session.created":
            self._current_session_id = event.session_id
            self.chat.set_title(event.title)
            self.chat.messages.clear()
            self.stack.setCurrentWidget(self.chat)
        elif t == "session.events":
            self._current_session_id = event.session_id
            self.chat.load_session(self._session_titles.get(event.session_id, ""), event.events)
            self.stack.setCurrentWidget(self.chat)
        elif t == "msg.assistant.delta":
            self.chat.on_delta(event)
        elif t == "msg.assistant.final":
            self.chat.on_final(event)
        elif t == "turn.status":
            self.chat.on_status(event)
        elif t == "error":
            self.chat.on_error(event)
        elif t == "provider.list":
            self.models.update_providers(event.providers, event.slots)
            self.chat.set_models(event.providers, event.slots.get("main"))
        elif t == "provider.test.result":
            self.models.on_test_result(event)
        elif t == "settings.state":
            ui = event.data.get("ui", {})
            self._apply_appearance(ui.get("theme"), ui.get("font_size"))
            self.settings.load_settings(event.data)
