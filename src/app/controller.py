"""请求分派（core 线程侧）。

把来自 BusBridge 的请求信封分派到各子系统，并把结果事件经 bridge 回发。
核心线程持有「当前会话」状态；GUI 侧不持有 core 内部对象。
"""

from __future__ import annotations

import logging
from pathlib import Path

from shared.envelope import (
    ArchiveSession,
    CancelTurn,
    DeleteSession,
    ErrorReport,
    HealthReport,
    NewSession,
    ProviderDelete,
    ProviderList,
    ProviderUpsert,
    RenameSession,
    ResumeSession,
    SendMessage,
    SessionCreated,
    SessionEvents,
    SessionIndex,
    SettingsState,
    SettingsUpdate,
    SwitchModel,
    TestConnection,
    UnarchiveSession,
)
from shared.schema import (
    ContextSettings,
    LoggingSettings,
    NetworkSettings,
    UISettings,
)

from core.agent.loop import AgentLoop
from core.agent.session import SessionStore
from core.bus.bridge import BusBridge
from core.gateway.provider import ModelGateway
from core.modules.supervisor import ModuleSupervisor
from core.registry.registry import Registry
from core.store.config_store import ConfigStore

log = logging.getLogger(__name__)


class CoreController:
    def __init__(
        self,
        bridge: BusBridge,
        store: SessionStore,
        gateway: ModelGateway,
        agent: AgentLoop,
        supervisor: ModuleSupervisor,
        registry: Registry,
        config_store: ConfigStore,
        root: Path,
    ) -> None:
        self.bridge = bridge
        self.store = store
        self.gateway = gateway
        self.agent = agent
        self.supervisor = supervisor
        self.registry = registry
        self.config_store = config_store
        self.root = Path(root)
        self.current_session_id: str | None = None

    # -- 发射辅助 ----------------------------------------------------------
    def emit(self, event) -> None:
        self.bridge.emit_event(event)

    def _emit_providers(self) -> None:
        self.emit(ProviderList(providers=self.gateway.list_providers(), slots=self.gateway.get_slots()))

    def _emit_index(self) -> None:
        self.emit(SessionIndex(sessions=self.store.list(include_archived=True)))

    def _emit_health(self) -> None:
        slots = self.gateway.get_slots()
        self.emit(
            HealthReport(
                modules=self.supervisor.get_states(),
                main_model=slots.get("main"),
                slot_ready=slots.get("main") is not None,
            )
        )

    def push_initial_state(self) -> None:
        """GUI 连接信号后调用，推送首屏数据。"""
        self._emit_providers()
        self._emit_index()
        self._emit_settings()
        self._emit_health()

    def _emit_settings(self) -> None:
        settings = self.config_store.load("settings")
        self.emit(SettingsState(data=settings.model_dump(mode="json")))

    # -- 分派 --------------------------------------------------------------
    def handle(self, request) -> None:
        t = getattr(request, "type", None)
        if t == "msg.user":
            self._on_send(request)
        elif t == "turn.cancel":
            self._on_cancel(request)
        elif t == "model.switch":
            self._on_switch(request)
        elif t == "session.new":
            self._on_new(request)
        elif t == "session.resume":
            self._on_resume(request)
        elif t == "session.archive":
            self._on_archive(request)
        elif t == "session.unarchive":
            self._on_unarchive(request)
        elif t == "session.rename":
            self._on_rename(request)
        elif t == "session.delete":
            self._on_delete(request)
        elif t == "provider.upsert":
            self._on_upsert(request)
        elif t == "provider.delete":
            self._on_provider_delete(request)
        elif t == "provider.test":
            self._on_test(request)
        elif t == "settings.update":
            self._on_settings(request)
        else:
            log.warning("未知请求类型：%s", t)

    # -- 会话 --------------------------------------------------------------
    def _ensure_session(self) -> str:
        if self.current_session_id is None:
            meta = self.store.create(None, None)
            self.current_session_id = meta.id
            self.emit(SessionCreated(session_id=meta.id, title=meta.title, created_at=meta.created_at))
            self._emit_index()
        return self.current_session_id

    def _on_send(self, request: SendMessage) -> None:
        session_id = self._ensure_session()
        self.agent.run_turn(session_id, request)

    def _on_cancel(self, request: CancelTurn) -> None:
        if self.current_session_id:
            self.agent.cancel(self.current_session_id)

    def _on_switch(self, request: SwitchModel) -> None:
        if not self.current_session_id:
            return
        self.store.set_model(self.current_session_id, request.model_id, request.slot)
        self._emit_health()

    def _on_new(self, request: NewSession) -> None:
        meta = self.store.create(request.title, request.persona_id)
        self.current_session_id = meta.id
        self.emit(SessionCreated(session_id=meta.id, title=meta.title, created_at=meta.created_at))
        self._emit_index()

    def _on_resume(self, request: ResumeSession) -> None:
        try:
            snapshot = self.store.resume(request.session_id)
        except KeyError:
            self.emit(ErrorReport(scope="session", code="session_not_found", message="会话不存在"))
            return
        self.current_session_id = request.session_id
        self.emit(SessionEvents(session_id=request.session_id, events=snapshot.events))
        self._emit_health()

    def _on_archive(self, request: ArchiveSession) -> None:
        try:
            self.store.archive(request.session_id)
        except KeyError:
            pass
        self._emit_index()

    def _on_unarchive(self, request: UnarchiveSession) -> None:
        try:
            self.store.unarchive(request.session_id)
        except KeyError:
            pass
        self._emit_index()

    def _on_rename(self, request: RenameSession) -> None:
        try:
            self.store.rename(request.session_id, request.title)
        except KeyError:
            pass
        self._emit_index()

    def _on_delete(self, request: DeleteSession) -> None:
        self.store.delete(request.session_id)
        if self.current_session_id == request.session_id:
            self.current_session_id = None
        self._emit_index()

    # -- 供应商 ------------------------------------------------------------
    def _on_upsert(self, request: ProviderUpsert) -> None:
        self.gateway.upsert_provider(request.provider, request.api_key)
        self._emit_providers()

    def _on_provider_delete(self, request: ProviderDelete) -> None:
        self.gateway.delete_provider(request.provider_id)
        self._emit_providers()

    def _on_test(self, request: TestConnection) -> None:
        ok, latency_ms, error = self.gateway.test_connection(request.provider_id, request.model_id)
        from shared.envelope import TestResult

        self.emit(
            TestResult(
                provider_id=request.provider_id,
                model_id=request.model_id,
                ok=ok,
                latency_ms=latency_ms,
                error=error,
            )
        )

    # -- 设置 --------------------------------------------------------------
    def _on_settings(self, request: SettingsUpdate) -> None:
        settings = self.config_store.load("settings")
        data = request.data
        if request.section == "context":
            settings.context = ContextSettings.model_validate({**settings.context.model_dump(), **data})
        elif request.section == "network":
            settings.network = NetworkSettings.model_validate({**settings.network.model_dump(), **data})
        elif request.section == "logging":
            settings.logging = LoggingSettings.model_validate({**settings.logging.model_dump(), **data})
        elif request.section == "ui":
            settings.ui = UISettings.model_validate({**settings.ui.model_dump(), **data})
        self.config_store.save("settings", settings)
        self.gateway.reload_settings()
        self._emit_settings()
