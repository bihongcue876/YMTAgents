"""请求分派（core 线程侧）。

把来自 BusBridge 的请求信封分派到各子系统，并把结果事件经 bridge 回发。
核心线程持有「当前会话」状态；GUI 侧不持有 core 内部对象。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from shared.errors import ErrorCode, error_text
from shared.redact import redact
from shared.envelope import (
    ArchiveSession,
    CancelTurn,
    DeleteSession,
    ErrorReport,
    FetchModels,
    HealthReport,
    NewSession,
    ProviderDelete,
    ProviderList,
    ProviderModels,
    ProviderUpsert,
    RenameSession,
    ResumeSession,
    SendMessage,
    SessionCreated,
    SessionEvents,
    SessionIndex,
    SetSlot,
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

from app import logging_setup

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

    def _report(self, scope: str, code: str, message: str, detail: str | None = None) -> None:
        """回发错误事件。message 必须是可读中文（不得以码充文案，spec rev5 §4）。

        detail 一律过脱敏（spec rev9 §3）：错误信息是最容易夹带密钥的出口，
        且 error 事件会落进 events.jsonl，一旦夹带即持久化。
        """
        self.emit(ErrorReport(scope=scope, code=code, message=message, detail=redact(detail)))

    def _persist(
        self,
        action: str,
        fn: Callable[[], object],
        message: str,
        scope: str = "config",
    ) -> bool:
        """执行一次落盘/凭据写入动作；失败**必须**上报，不得只留日志（spec rev8 §5）。

        边界处统一收口：凭据管理器与文件系统的异常类型名不可控，故此处宽捕获，
        明细进日志（去敏：不把异常正文回显给界面，避免带出路径与凭据信息）。
        """
        try:
            fn()
            return True
        except Exception as exc:  # noqa: BLE001 - 边界收口，异常明细只进日志
            log.exception("%s 失败", action)
            self._report(scope, ErrorCode.STORAGE_ERROR.value, message, type(exc).__name__)
            return False

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
        elif t == "slot.set":
            self._on_set_slot(request)
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
        elif t == "provider.models":
            self._on_fetch_models(request)
        elif t == "settings.update":
            self._on_settings(request)
        else:
            # 未知类型**不得静默**：此前只写一条 warning，调用方拿不到任何反馈（spec rev9 §1）。
            log.warning("未知请求类型：%s", t)
            self._report(
                "system",
                ErrorCode.INVALID_REQUEST.value,
                "请求格式不合法：未知请求类型。",
                f"type={t!r}",
            )

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
        # 会话级覆盖目前只落地 main 槽位（`SessionMeta` 只有 `main_model`，其余槽位随轮次启用）。
        # 早前实现把任何槽位的模型都写进 `main_model`：既污染主模型，又让 `model.switch`
        # 事件声称的槽位与生效对象不一致（spec rev8 §3）。此处显式拒绝而非静默改错对象。
        if request.slot != "main":
            self._report(
                "session",
                ErrorCode.INVALID_REQUEST.value,
                f"会话级模型切换目前仅支持 main 槽位；{request.slot} 随轮次启用。",
            )
            return
        self.store.set_model(self.current_session_id, request.model_id, request.slot)
        self._emit_health()

    def _on_set_slot(self, request: SetSlot) -> None:
        """全局槽位绑定（spec rev4 §3）。

        与 _on_switch 的作用域区分：本方法写 models.json 的 slots（配置层，全局生效），
        _on_switch 写会话 meta（会话实例层）。model_id=None 即清空该槽位绑定。
        绑定是用户直接操作（同类于 09 §7 白名单编辑），不走过确认关卡；
        归属解析留给调用路径，失败以 provider_not_found 呈现（rev1 §7）。
        """
        self._persist(
            "全局槽位绑定",
            lambda: self.gateway.set_slot(request.slot, request.model_id),
            "槽位绑定保存失败：请检查数据目录是否可写。",
        )
        self._emit_providers()
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
        self._persist(
            "供应商写入",
            lambda: self.gateway.upsert_provider(request.provider, request.api_key),
            "供应商保存失败：请检查系统凭据管理器与数据目录是否可用。",
        )
        self._emit_providers()

    def _on_provider_delete(self, request: ProviderDelete) -> None:
        self._persist(
            "供应商删除",
            lambda: self.gateway.delete_provider(request.provider_id),
            "供应商删除失败：请检查数据目录是否可写。",
        )
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

    def _on_fetch_models(self, request: FetchModels) -> None:
        """拉取端点自报的模型列表（spec rev9 §2）。

        只读探测：结果经 `ProviderModels` 回发，**不写任何配置** —— 是否登记由用户在界面上决定。
        """
        ok, models, error = self.gateway.list_remote_models(request.provider_id)
        self.emit(
            ProviderModels(provider_id=request.provider_id, ok=ok, models=models, error=error)
        )

    # -- 设置 --------------------------------------------------------------
    def _on_settings(self, request: SettingsUpdate) -> None:
        settings = self.config_store.load("settings")
        data = request.data
        try:
            if request.section == "context":
                settings.context = ContextSettings.model_validate(
                    {**settings.context.model_dump(), **data}
                )
            elif request.section == "network":
                settings.network = NetworkSettings.model_validate(
                    {**settings.network.model_dump(), **data}
                )
            elif request.section == "logging":
                settings.logging = LoggingSettings.model_validate(
                    {**settings.logging.model_dump(), **data}
                )
            elif request.section == "ui":
                settings.ui = UISettings.model_validate({**settings.ui.model_dump(), **data})
        except ValidationError as exc:
            # A10：schema 不符必须回 invalid_request。早前此处异常直抛，被核心线程整条吞掉：
            # 设置既未落盘、也无任何提示，界面停在无效取值上（spec rev8 §4）。
            self._report(
                "system",
                ErrorCode.INVALID_REQUEST.value,
                f"设置取值不合法（{request.section} 分区），已保持原值。",
                str(exc)[:300],
            )
            return
        if not self._persist(
            "设置写入",
            lambda: self.config_store.save("settings", settings),
            "设置保存失败：请检查数据目录是否可写。",
        ):
            return
        if request.section == "logging":
            # 日志级别**即时生效**：此前只落盘，从不应用（rev9 §4）。
            logging_setup.apply_level(settings.logging.level)
        self.gateway.reload_settings()
        self._emit_settings()

    # -- 退出收口 ----------------------------------------------------------
    def shutdown(self) -> None:
        """结束当前会话并把事件流 fsync 到磁盘（docs 03 §10）。

        由应用入口在**核心线程停止之后**调用，故此处不存在并发写；
        退出路径不得再向外抛异常。
        """
        session_id = self.current_session_id
        if not session_id:
            return
        try:
            self.store.end(session_id, "app_exit")
        except Exception:  # noqa: BLE001 - 退出路径不抛
            log.exception("会话收尾失败")
