"""请求分派（core 线程侧）。

把来自 BusBridge 的请求信封分派到各子系统，并把结果事件经 bridge 回发。
核心线程持有「当前会话」状态；GUI 侧不持有 core 内部对象。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from shared.errors import ErrorCode, error_text
from shared.redact import redact
from shared.envelope import (
    ArchiveSession,
    CancelTurn,
    ContextUsage,
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
    SessionDetail,
    SessionDetailResult,
    SessionEvents,
    SessionIndex,
    SessionUpdate,
    CompressMemory,
    BranchSession,
    RevertSession,
    SwitchBranch,
    SessionBranches,
    BranchInfo,
    SetSlot,
    SettingsState,
    SettingsUpdate,
    SwitchModel,
    TestConnection,
    UnarchiveSession,
    PersonaDelete,
    PersonaInfo,
    PersonaList,
    PersonaSave,
    PersonaSetDefault,
    PersonaSwitch,
    PersonaExport,
    PersonaExported,
    PersonaImport,
    PersonaImported,
    McpServerList,
    McpServerStatus,
    SkillImported,
    SkillList,
    ToolList,
)
from shared.schema import (
    LoggingSettings,
    McpServerConfig,
    NetworkSettings,
    UISettings,
)

from core.agent.loop import AgentLoop, model_ctx_window
from core.agent.session import MAX_BRANCHES, SessionStore
from core.agent.persona import PersonaStore, YMT_PERSONA_ID
from core.agent.memory import effective_switches, effective_threshold, recommended_range
from core.bus.bridge import BusBridge
from core.gateway.errors import GatewayError
from core.gateway.provider import ModelGateway
from core.mcp.manager import McpManager
from core.modules.supervisor import ModuleSupervisor
from core.registry.executor import GATE_TIMEOUT_S, ToolExecutor
from core.registry.registry import Registry
from core.store.config_store import ConfigStore

from app import logging_setup
from shared.errors import error_text

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
        personas: PersonaStore | None = None,
        executor: ToolExecutor | None = None,
        mcp_manager: McpManager | None = None,
        skill_manager=None,
    ) -> None:
        self.bridge = bridge
        self.store = store
        self.gateway = gateway
        self.agent = agent
        self.supervisor = supervisor
        self.registry = registry
        self.config_store = config_store
        self.root = Path(root)
        self.personas = personas
        self.executor = executor
        self.mcp_manager = mcp_manager
        self.skill_manager = skill_manager
        self.current_session_id: str | None = None
        # rev43：confirm 关卡裁决登记（泵取队列时命中；正常分派路径亦可投递）。
        self._gate_decisions: dict[str, bool] = {}
        if self.executor is not None:
            self.executor.set_gate(self._gate_handler)

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
        """执行一次落盘/密钥写入动作；失败**必须**上报，不得只留日志（spec rev8 §5）。

        边界处统一收口：本地加密库与文件系统的异常类型名不可控，故此处宽捕获，
        明细进日志（去敏：不把异常正文回显给界面，避免带出路径与凭据信息）。
        """
        try:
            fn()
            return True
        except GatewayError as exc:
            # 配置层的网关校验（如 rev15 明文传输拦截）按**真实码**上报，
            # 不得吞成 storage_error —— 那会把「地址不安全」误导成「磁盘坏了」。
            log.info("%s 被拒绝：%s", action, exc.code)
            self._report(scope, exc.code, str(exc) or error_text(exc.code), None)
            return False
        except Exception as exc:  # noqa: BLE001 - 边界收口，异常明细只进日志
            log.exception("%s 失败", action)
            self._report(scope, ErrorCode.STORAGE_ERROR.value, message, type(exc).__name__)
            return False

    def _emit_providers(self) -> None:
        self.emit(ProviderList(providers=self.gateway.list_providers(), slots=self.gateway.get_slots()))

    def _emit_index(self) -> None:
        sessions = self.store.list(include_archived=True)
        if self.personas is not None:
            names = {p.id: p.name for p in self.personas.list()}
            for s in sessions:
                s.persona_name = names.get(s.persona_id) if s.persona_id else None
        self.emit(SessionIndex(sessions=sessions))

    def _emit_health(self) -> None:
        self.refresh_modules()
        slots = self.gateway.get_slots()
        self.emit(
            HealthReport(
                modules=self.supervisor.get_states(),
                main_model=slots.get("main"),
                slot_ready=slots.get("main") is not None,
            )
        )

    def refresh_modules(self) -> None:
        """把宿主态同步进 supervisor（rev44：mcp 模块实装）。"""
        if self.mcp_manager is not None:
            self.supervisor.set_state("mcp", self.mcp_manager.host_state())

    def push_initial_state(self) -> None:
        """GUI 连接信号后调用，推送首屏数据。"""
        self._emit_providers()
        self._emit_index()
        self._emit_settings()
        self._emit_health()
        self._emit_personas()
        self._emit_mcp()
        self._emit_skills()

    # -- Persona（阶段 2 · spec rev23） --------------------------------------
    def _emit_personas(self) -> None:
        """推送角色库 + 全局默认 + 当前会话所用（用于角色页与头条下拉）。"""
        if self.personas is None:
            return
        session_persona = None
        if self.current_session_id:
            try:
                session_persona = self.store.get_meta(self.current_session_id).persona_id
            except KeyError:
                session_persona = None
        infos = [
            PersonaInfo(
                id=p.id,
                name=p.name,
                builtin=p.builtin,
                prompt=p.prompt,
                is_default=(p.id == self.personas.current_default()),
                in_session=(session_persona is not None and p.id == session_persona),
            )
            for p in self.personas.list()
        ]
        self.emit(PersonaList(personas=infos))

    def _on_persona_export(self, request: PersonaExport) -> None:
        """导出角色到用户选定路径（rev32）；写盘在 core，失败给可读回报。"""
        if self.personas is None:
            return
        try:
            name = self.personas.export_persona(request.persona_id, request.path)
        except KeyError:
            self.emit(
                PersonaExported(
                    persona_id=request.persona_id, ok=False, path=request.path, error="角色不存在。"
                )
            )
            return
        except OSError:
            self.emit(
                PersonaExported(
                    persona_id=request.persona_id,
                    ok=False,
                    path=request.path,
                    error="导出失败：请检查目标路径是否可写。",
                )
            )
            return
        self.emit(
            PersonaExported(persona_id=request.persona_id, ok=True, path=request.path, name=name)
        )

    def _on_persona_import(self, request: PersonaImport) -> None:
        """从文件新建角色（rev32）；按不可信输入校验，失败不落盘。"""
        if self.personas is None:
            return
        try:
            pid, name = self.personas.import_persona(request.path)
        except ValueError as exc:
            message = {
                "unreadable": "无法读取导入文件。",
                "file_too_large": "导入文件过大（上限 2 MB）。",
                "invalid_persona_file": "导入文件不是有效的角色文件。",
            }.get(str(exc), "导入失败：文件无效。")
            self.emit(PersonaImported(ok=False, path=request.path, error=message))
            return
        self.emit(PersonaImported(ok=True, path=request.path, persona_id=pid, name=name))
        self._emit_personas()

    def _on_persona_save(self, request: PersonaSave) -> None:
        """新建或更新角色；落盘失败必须上报（rev8 §5 口径）。"""
        if not request.name.strip():
            self._report("config", ErrorCode.INVALID_REQUEST.value, "角色名称不能为空。")
            return
        if self.personas is None:
            return
        pid = self._persist(
            "保存角色",
            lambda: self.personas.save(request.persona_id, request.name.strip(), request.prompt),
            "角色保存失败：请检查数据目录是否可写。",
        )
        if pid:
            self._emit_personas()

    def _on_persona_delete(self, request: PersonaDelete) -> None:
        if self.personas is None:
            return
        if request.persona_id == YMT_PERSONA_ID:
            self._report("config", ErrorCode.INVALID_REQUEST.value, "YMT 预置角色不可删除。")
            return
        deleted = self._persist(
            "删除角色",
            lambda: self.personas.delete(request.persona_id),
            "角色删除失败：请检查数据目录是否可写。",
        )
        if deleted:
            self._emit_personas()

    def _on_persona_set_default(self, request: PersonaSetDefault) -> None:
        if self.personas is None:
            return
        ok = self._persist(
            "设置默认角色",
            lambda: self.personas.set_current_default(request.persona_id),
            "设置默认角色失败。",
        )
        if ok:
            self._emit_personas()

    def _on_persona_switch(self, request: PersonaSwitch) -> None:
        """当前会话切换角色（会话级；无会话则更新全局默认，供下一个对话使用）。"""
        if self.personas is None:
            return
        if self.personas.get(request.persona_id) is None:
            self._report("config", ErrorCode.INVALID_REQUEST.value, "角色不存在。")
            return
        if self.current_session_id:
            self.store.set_persona(self.current_session_id, request.persona_id)
            self._emit_index()  # 侧栏/索引里的 persona_name 随之刷新
        else:
            self._persist(
                "设置默认角色",
                lambda: self.personas.set_current_default(request.persona_id),
                "设置默认角色失败。",
            )
        self._emit_personas()

    def _emit_settings(self) -> None:
        settings = self.config_store.load("settings")
        self.emit(SettingsState(data=settings.model_dump(mode="json")))

    # -- MCP / 工具（rev41/rev43/rev44） -------------------------------------
    def _emit_mcp(self) -> None:
        if self.mcp_manager is None:
            return
        self.emit(McpServerList(servers=self.mcp_manager.list_status()))
        self.emit(ToolList(tools=self.mcp_manager.tool_entries()))

    def _on_mcp_upsert(self, request) -> None:
        if self.mcp_manager is None:
            return
        try:
            config = McpServerConfig.model_validate(request.server)
        except ValidationError as exc:
            self._report(
                "config",
                ErrorCode.INVALID_REQUEST.value,
                "MCP 服务器配置不合法。",
                redact(str(exc))[:500],
            )
            return
        self._persist(
            "mcp.server.upsert", lambda: self.mcp_manager.upsert(config), "保存 MCP 服务器失败。"
        )
        self._emit_mcp()
        self._emit_health()

    def _on_mcp_delete(self, request) -> None:
        if self.mcp_manager is not None:
            self._persist(
                "mcp.server.delete",
                lambda: self.mcp_manager.delete(request.id),
                "删除 MCP 服务器失败。",
            )
            self._emit_mcp()
            self._emit_health()

    def _on_mcp_toggle(self, request) -> None:
        if self.mcp_manager is not None:
            self._persist(
                "mcp.server.toggle",
                lambda: self.mcp_manager.set_enabled(request.id, request.enabled),
                "切换 MCP 服务器失败。",
            )
            self._emit_mcp()
            self._emit_health()

    def _on_mcp_reconnect(self, request) -> None:
        if self.mcp_manager is not None:
            self.mcp_manager.reconnect(request.id)
            self._emit_mcp()
            self._emit_health()

    def _on_gate_respond(self, request) -> None:
        # 正常路径下 gate.respond 多被 _gate_handler 泵取命中；此处登记以兜底竞态。
        self._gate_decisions[request.call_id] = request.decision == "allow"

    # -- Skills（v0.0.4） -----------------------------------------------------
    def _emit_skills(self) -> None:
        if self.skill_manager is None:
            return
        self.emit(SkillList(skills=self.skill_manager.list_status()))

    def _on_skill_toggle(self, request) -> None:
        if self.skill_manager is None:
            return
        try:
            self.skill_manager.toggle(request.id, request.enabled)
        except ValueError as exc:
            self._report("config", ErrorCode.INVALID_REQUEST.value, str(exc))
            return
        self._emit_skills()

    def _on_skill_import(self, request) -> None:
        if self.skill_manager is None:
            return
        try:
            metas = self.skill_manager.import_skill(request.source)
        except ValueError as exc:
            self.emit(SkillImported(ok=False, source=request.source, error=str(exc)))
            return
        except Exception:  # noqa: BLE001 - 边界收口，明细只进日志
            log.exception("技能导入失败")
            self.emit(SkillImported(ok=False, source=request.source, error="导入失败：磁盘写入异常。"))
            return
        self.emit(
            SkillImported(
                ok=True,
                source=request.source,
                skill_ids=[m.id for m in metas],
                names=[m.name for m in metas],
            )
        )
        self._emit_skills()

    def _on_skill_update(self, request) -> None:
        if self.skill_manager is None:
            return
        try:
            meta = self.skill_manager.update_skill(request.id)
        except ValueError as exc:
            self.emit(SkillImported(ok=False, source=request.id, updated=True, error=str(exc)))
            return
        except Exception:  # noqa: BLE001 - 边界收口
            log.exception("技能更新失败")
            self.emit(SkillImported(ok=False, source=request.id, updated=True, error="更新失败：磁盘写入异常。"))
            return
        self.emit(SkillImported(ok=True, source=request.id, skill_ids=[meta.id], names=[meta.name], updated=True))
        self._emit_skills()

    def _on_skill_delete(self, request) -> None:
        if self.skill_manager is None:
            return
        try:
            self.skill_manager.delete(request.id)
        except ValueError as exc:
            self._report("config", ErrorCode.INVALID_REQUEST.value, str(exc))
            return
        self._emit_skills()

    def _on_skill_permission(self, request) -> None:
        if self.skill_manager is None:
            return
        try:
            self.skill_manager.set_permission(request.id, request.permission)
        except ValueError as exc:
            self._report("config", ErrorCode.INVALID_REQUEST.value, str(exc))
            return
        self._emit_skills()

    def _gate_handler(self, call_id: str, name: str, args: dict, permission: str) -> bool:
        """confirm 关卡：泵取请求队列直到收到本次 call_id 的 gate.respond。

        回合运行于核心线程；若仅阻塞等待，gate.respond 永远排不到队列头（单线程 FIFO），
        故此处自行泵取：命中决策即返回；turn.cancel 记为**拒绝**（中断在途确认一律拒绝）；
        其余请求暂存，关卡结束后回投队列。
        """
        if call_id in self._gate_decisions:
            return self._gate_decisions.pop(call_id)
        deadline = time.monotonic() + GATE_TIMEOUT_S
        deferred = []
        try:
            while True:
                if call_id in self._gate_decisions:
                    return self._gate_decisions.pop(call_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False  # 超时 = 拒绝（docs 09 §3）
                request = self.bridge.get(timeout=min(0.2, remaining))
                if request is None:
                    continue
                t = getattr(request, "type", None)
                if t == "gate.respond":
                    if request.call_id == call_id:
                        return request.decision == "allow"
                    continue
                if t == "turn.cancel":
                    self._on_cancel(request)
                    return False
                deferred.append(request)
        finally:
            for request in deferred:
                self.bridge.submit(request)

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
        elif t == "session.detail":
            self._on_detail(request)
        elif t == "session.update":
            self._on_update(request)
        elif t == "session.compress":
            self._on_compress(request)
        elif t == "session.branch":
            self._on_branch(request)
        elif t == "session.revert":
            self._on_revert(request)
        elif t == "session.switch_branch":
            self._on_switch_branch(request)
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
        elif t == "persona.list":
            self._emit_personas()
        elif t == "persona.save":
            self._on_persona_save(request)
        elif t == "persona.delete":
            self._on_persona_delete(request)
        elif t == "persona.set_default":
            self._on_persona_set_default(request)
        elif t == "persona.switch":
            self._on_persona_switch(request)
        elif t == "persona.export":
            self._on_persona_export(request)
        elif t == "persona.import":
            self._on_persona_import(request)
        elif t == "mcp.server.upsert":
            self._on_mcp_upsert(request)
        elif t == "mcp.server.delete":
            self._on_mcp_delete(request)
        elif t == "mcp.server.toggle":
            self._on_mcp_toggle(request)
        elif t == "mcp.server.reconnect":
            self._on_mcp_reconnect(request)
        elif t == "mcp.server.refresh":
            self._emit_mcp()
        elif t == "skill.toggle":
            self._on_skill_toggle(request)
        elif t == "skill.import":
            self._on_skill_import(request)
        elif t == "skill.update":
            self._on_skill_update(request)
        elif t == "skill.delete":
            self._on_skill_delete(request)
        elif t == "skill.permission":
            self._on_skill_permission(request)
        elif t == "skill.refresh":
            self._emit_skills()
        elif t == "gate.respond":
            self._on_gate_respond(request)
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
            persona_id = self.personas.current_default() if self.personas else None
            meta = self.store.create(None, persona_id)
            self.current_session_id = meta.id
            self.emit(SessionCreated(session_id=meta.id, title=meta.title, created_at=meta.created_at))
            self._emit_index()
            self._emit_personas()
        return self.current_session_id

    def _on_send(self, request: SendMessage) -> None:
        session_id = self._ensure_session()
        self.agent.run_turn(session_id, request)
        self._emit_detail(session_id)  # 回合结束后刷新右栏（含最近一次上下文用量）

    def _on_cancel(self, request: CancelTurn) -> None:
        if self.current_session_id:
            self.agent.cancel(self.current_session_id)

    def _on_switch(self, request: SwitchModel) -> None:
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
        # rev23 语义（对齐 Coding agents 平台，修订 rev14）：对话内选择只属于**该对话**
        # （单对话选择、单对话不一致），不再强制登记「上次使用」；
        # 新对话的全局默认改由「无会话时的选择」或模型页的显式设置决定。
        # 无会话时仍写全局默认：那是「为下一个对话选默认」的合法入口。
        if self.current_session_id:
            self.store.set_model(self.current_session_id, request.model_id, request.slot)
            self._emit_index()  # 侧栏/下拉缓存随会话级选择刷新（rev23）
        elif request.model_id != self.gateway.get_slots().get("main"):
            self._persist(
                "设置新对话的默认模型",
                lambda: self.gateway.set_slot("main", request.model_id),
                "设置默认模型失败。",
            )
        self._emit_providers()
        self._emit_health()
        if self.current_session_id:
            self._emit_detail(self.current_session_id)  # 换模型 → 生效窗口变化

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
        # rev23：新会话默认用「全局默认角色」；请求显式指定则用指定的
        persona_id = request.persona_id or (
            self.personas.current_default() if self.personas else None
        )
        meta = self.store.create(request.title, persona_id)
        self.current_session_id = meta.id
        self.emit(SessionCreated(session_id=meta.id, title=meta.title, created_at=meta.created_at))
        self._emit_index()
        self._emit_personas()  # 新会话的 in_session 标记变了
        self._emit_branches(meta.id)
        self._emit_detail(meta.id)

    def _on_resume(self, request: ResumeSession) -> None:
        try:
            snapshot = self.store.resume(request.session_id)
        except KeyError:
            self.emit(ErrorReport(scope="session", code=ErrorCode.SESSION_NOT_FOUND.value, message="会话不存在"))
            return
        self.current_session_id = request.session_id
        self.emit(SessionEvents(session_id=request.session_id, events=snapshot.events))
        self._emit_branches(request.session_id)
        self._emit_health()
        self._emit_personas()  # 当前会话变了 → in_session 标记刷新（rev23）
        self._emit_detail(request.session_id)

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

    # -- 会话详情 / 策略（stage 2 · rev24） ---------------------------------
    def _effective_window(self, meta) -> int:
        """本会话实际生效窗口：会话自设上限优先，否则用所选模型声明值（0=未知）。"""
        model_id = meta.main_model or self.gateway.get_slots().get("main")
        if not model_id:
            return 0
        return meta.max_context or model_ctx_window(self.gateway, model_id)

    def _emit_detail(self, session_id: str) -> None:
        try:
            meta = self.store.get_meta(session_id)
        except KeyError:
            return
        events = self.store.replay(session_id)
        user_count = sum(1 for e in events if e.get("type") == "msg.user")
        assistant_count = sum(1 for e in events if e.get("type") == "msg.assistant.final")
        cumulative = 0
        for event in events:
            if event.get("type") != "msg.assistant.final":
                continue
            usage = (event.get("payload") or {}).get("usage") or {}
            cumulative += int(usage.get("total_tokens") or 0)
        last_usage: ContextUsage | None = None
        for event in reversed(events):
            if event.get("type") == "ctx.usage":
                try:
                    last_usage = ContextUsage.model_validate(event.get("payload", {}))
                except ValidationError:
                    last_usage = None
                break
        memory = self.store.read_memory(session_id)
        memory_config = self.config_store.load("memory")
        threshold = effective_threshold(meta, memory_config)
        memory_use, memory_compress, memory_auto = effective_switches(meta, memory_config)
        rec_min, rec_max = recommended_range(self._effective_window(meta), memory_config)
        graph = self.store.list_branches(session_id)
        self.emit(
            SessionDetailResult(
                session_id=session_id,
                meta=meta,
                turn_count=user_count,
                user_count=user_count,
                assistant_count=assistant_count,
                data_bytes=self.store.data_bytes(session_id),
                effective_window=self._effective_window(meta),
                last_usage=last_usage,
                cumulative_tokens=cumulative,
                memory_revision=(memory.revision if memory else 0),
                memory_covered_seq=(memory.covered_seq if memory else -1),
                memory_tokens=(memory.tokens_est if memory else 0),
                memory_use=memory_use,
                memory_compress=memory_compress,
                memory_auto=memory_auto,
                memory_threshold=threshold,
                memory_recommended_min=rec_min,
                memory_recommended_max=rec_max,
                memory_history=self.store.list_memory_history(session_id),
                branch_count=len(graph.branches),
                active_branch=graph.active,
                max_branches=MAX_BRANCHES,
            )
        )

    def _on_detail(self, request: SessionDetail) -> None:
        self._emit_detail(request.session_id)

    def _on_update(self, request: SessionUpdate) -> None:
        title = request.title.strip()
        if not title:
            self._report("session", ErrorCode.INVALID_REQUEST.value, "会话名称不能为空。")
            return
        if request.max_context is not None and request.max_context < 0:
            self._report("session", ErrorCode.INVALID_REQUEST.value, "上下文上限不能为负数。")
            return
        if request.memory_threshold is not None and not 50 <= request.memory_threshold <= 99:
            self._report(
                "session", ErrorCode.INVALID_REQUEST.value, "记忆压缩阈值需在 50–99 之间。"
            )
            return
        try:
            self.store.update(
                request.session_id,
                title=title,
                note=request.note.strip(),
                max_context=request.max_context,
                params=request.params,
                memory_use=request.memory_use,
                memory_compress=request.memory_compress,
                memory_auto=request.memory_auto,
                memory_threshold=request.memory_threshold,
            )
        except KeyError:
            self._report("session", ErrorCode.SESSION_NOT_FOUND.value, "会话不存在。")
            return
        self._emit_index()
        self._emit_detail(request.session_id)

    def _on_compress(self, request: CompressMemory) -> None:
        """压缩较早历史为记忆（v0.0.1）：结果由 loop 以 `session.memory.result` 回报。"""
        try:
            self.store.get_meta(request.session_id)
        except KeyError:
            self._report("session", ErrorCode.SESSION_NOT_FOUND.value, "会话不存在。")
            return
        self.agent.compress_memory(request.session_id, force=request.force)
        self._emit_detail(request.session_id)  # 记忆状态/用量刷新右栏

    # -- 分支树 / 回退（rev31） ---------------------------------------------
    def _emit_branches(self, session_id: str) -> None:
        branches = self.store.branch_info(session_id)
        active = next((b.id for b in branches if b.active), "br0")
        self.emit(
            SessionBranches(
                session_id=session_id,
                branches=branches,
                active=active,
                max_branches=MAX_BRANCHES,
            )
        )

    def _refresh_branch_view(self, session_id: str) -> None:
        """分支/回退改变了活动转录：重推事件流、分支树与详情。"""
        try:
            snapshot = self.store.resume(session_id)
        except KeyError:
            return
        self.emit(SessionEvents(session_id=session_id, events=snapshot.events))
        self._emit_branches(session_id)
        self._emit_detail(session_id)

    def _on_branch(self, request: BranchSession) -> None:
        try:
            self.store.create_branch(request.session_id, request.from_seq)
        except KeyError:
            self._report(
                "session", ErrorCode.INVALID_REQUEST.value, "该消息不在当前分支中，无法从此处分支。"
            )
            return
        except ValueError:
            self._report(
                "session",
                ErrorCode.INVALID_REQUEST.value,
                f"分支数已达上限（{MAX_BRANCHES}），请先切换到已有分支再操作。",
            )
            return
        self._refresh_branch_view(request.session_id)

    def _on_revert(self, request: RevertSession) -> None:
        try:
            self.store.revert_to(request.session_id, request.to_seq)
        except KeyError:
            self._report(
                "session", ErrorCode.INVALID_REQUEST.value, "该消息不在当前分支中，无法回退。"
            )
            return
        self._refresh_branch_view(request.session_id)

    def _on_switch_branch(self, request: SwitchBranch) -> None:
        try:
            self.store.switch_branch(request.session_id, request.branch_id)
        except KeyError:
            self._report("session", ErrorCode.INVALID_REQUEST.value, "分支不存在。")
            return
        self._refresh_branch_view(request.session_id)

    # -- 供应商 ------------------------------------------------------------
    def _on_upsert(self, request: ProviderUpsert) -> None:
        self._persist(
            "供应商写入",
            lambda: self.gateway.upsert_provider(request.provider, request.api_key),
            "供应商保存失败：请检查本地加密库与数据目录是否可用。",
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
            if request.section == "network":
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
        # rev44：无论是否有当前会话，都要回收 MCP 子进程/连接（退出路径不抛）。
        if self.mcp_manager is not None:
            try:
                self.mcp_manager.shutdown()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                log.exception("MCP 收尾失败")
        if not session_id:
            return
        try:
            self.store.end(session_id, "app_exit")
        except Exception:  # noqa: BLE001 - 退出路径不抛
            log.exception("会话收尾失败")
