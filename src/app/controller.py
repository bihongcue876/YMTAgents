"""请求分派（core 线程侧）。

把来自 BusBridge 的请求信封分派到各子系统，并把结果事件经 bridge 回发。
核心线程持有「当前会话」状态；GUI 侧不持有 core 内部对象。
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pydantic import ValidationError

if TYPE_CHECKING:  # 类型专用：运行期不 import 宿主模块（关档不 import 铁律）
    from core.modules.manager import FeatureManager
    from core.library.manager import ILibraryService

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
    ProviderToggle,
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
    BuiltinToolState,
    BuiltinToolToggle,
    RetrievalRefresh,
    RetrievalConfigUpdate,
    RetrievalKeySet,
    RetrievalState,
    RetrievalTest,
    RevertSession,
    SwitchBranch,
    SessionBranches,
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
    McpScanResult,
    SkillImported,
    SkillList,
    ShellList,
    ToolList,
    WorkspaceCreate,
    WorkspaceDelete,
    WorkspaceDetail,
    WorkspaceDetailResult,
WorkspaceMemoryWrite,
    WorkspaceMemoryResult,
    WorkspaceBuild,
    WorkspaceBuildResult,
    WorkspaceFileRead,
    WorkspaceFileWrite,
    WorkspaceFileResult,
    WorkspaceInfo,
    WorkspaceList,
    WorkspaceRefresh,
    WorkspaceSwitch,
    WorkspaceUpdate,
    MoveSession,
    LibraryCreate,
    LibraryUpdate,
    LibraryDelete,
    LibrarySwitch,
    LibraryRefresh,
    LibraryDetail,
    LibraryIngest,
    LibraryQuery,
    FeatureState,
    BtcmState,
    LibraryList,
    LibraryDetailResult,
    LibraryIngestResult,
    LibraryQueryResult,
    LibraryGraphResult,
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
from core.modules.supervisor import ModuleSupervisor
from core.registry.executor import GATE_TIMEOUT_S, ToolContext, ToolExecutor
from core.registry.registry import Registry
from core.store.config_store import ConfigStore
from core.workspace.layout import WorkspaceDenied, WorkspacePathError

from app import logging_setup
from shared.ids import WS_DEFAULT

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
        features: FeatureManager | None = None,
        workspace_manager=None,
        files_manager=None,
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
        # 切片 0：附加功能生命周期是宿主的唯一入口；各 manager 经 `features.host()` 实时取。
        self.features = features
        self.workspace_manager = workspace_manager
        # v0.0.11：文件工具面（precheck 需要「当前工作区 root」→ 由这里按请求实时同步）。
        self.files_manager = files_manager
        self.current_session_id: str | None = None
        # rev43：confirm 关卡裁决登记（泵取队列时命中；正常分派路径亦可投递）。
        self._gate_decisions: dict[str, bool] = {}
        if self.executor is not None:
            self.executor.set_gate(self._gate_handler)

    # -- 附加功能宿主（切片 0：实时取，卸载后即为 None）--------------------
    @property
    def mcp_manager(self):
        return self.features.host("mcp") if self.features else None

    @property
    def shell_manager(self):
        return self.features.host("shell") if self.features else None

    @property
    def skill_manager(self):
        return self.features.host("skills") if self.features else None

    @property
    def library_manager(self) -> ILibraryService | None:
        """DPIM 管理面只在宿主启用时存在；属性本身不触发模块 import。"""
        return self.features.host("dpim") if self.features else None

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
        """把附加功能宿主态同步进 supervisor（切片 0 起统一走 `features`；卸载即 disabled）。"""
        for name in ("mcp", "shell", "btcm", "dpim", "retrieval"):
            host = self.features.host(name) if self.features is not None else None
            self.supervisor.set_state(name, host.host_state() if host is not None else "disabled")
        self._emit_retrieval()  # 检索分区首帧（关档也发空态，界面据 module_state 渲染）

    def _emit_features(self) -> None:
        if self.features is not None:
            self.emit(FeatureState(features=self.features.states()))

    def _on_feature_toggle(self, request) -> None:
        """二态滑动开关：切换宿主启停（关=真卸载、开=惰性装配），随后回推受影响的面。"""
        if self.features is None:
            return
        name, enabled = request.name, bool(request.enabled)
        try:
            ok = self.features.toggle(name, enabled)
        except ValueError:
            self._report(
                "system",
                ErrorCode.INVALID_REQUEST.value,
                "请求格式不合法：未知附加功能。",
                f"name={name!r}",
            )
            return
        if not ok:
            # 装配失败：真值已由 FeatureManager 回退；界面据下面的 feature.state 校准开关。
            self._report(
                "system",
                ErrorCode.INTERNAL.value,
                f"附加功能装配失败：{name}。",
                f"enabled={enabled}",
            )
        if name == "mcp":
            self._emit_mcp()
        elif name == "shell":
            self._emit_shell()
        elif name == "skills":
            self._emit_skills()
        elif name == "btcm":
            self._emit_btcm()
        elif name == "dpim":
            self._emit_libraries()
        elif name == "retrieval":
            self._emit_retrieval()
        self._emit_features()
        self._emit_health()

    def _emit_btcm(self) -> None:
        host = self.features.host("btcm") if self.features is not None else None
        if host is None:
            self.emit(BtcmState(ready=False))
            return
        payload = host.state_payload()
        self.emit(
            BtcmState(
                trigger=payload.get("trigger", "manual"),
                slot=payload.get("slot", "thinking"),
                ready=bool(payload.get("ready", True)),
            )
        )

    # -- Retrieval（检索模块，spec-2026-09-25-retrieval） ------------------------
    def _retrieval_host(self):
        """取检索宿主；模块关档 → invalid_request（不 import 实现，同 feature 先例）。"""
        host = self.features.host("retrieval") if self.features is not None else None
        if host is None:
            self._report("system", ErrorCode.INVALID_REQUEST.value, "检索模块未启用。", None)
        return host

    def _emit_retrieval(self) -> None:
        host = self.features.host("retrieval") if self.features is not None else None
        if host is None:
            self.emit(RetrievalState(engines=[], default_engines=[], module_state="disabled"))
            return
        host.state_event()

    def _on_retrieval_refresh(self, request) -> None:
        host = self._retrieval_host()
        if host is not None:
            host.state_event()

    def _on_retrieval_config(self, request) -> None:
        host = self._retrieval_host()
        if host is not None:
            host.config_update(request.engines, request.default_engines)

    def _on_retrieval_key(self, request) -> None:
        host = self._retrieval_host()
        if host is not None:
            host.key_set(request.engine, request.value)

    def _on_retrieval_test(self, request) -> None:
        host = self._retrieval_host()
        if host is not None:
            host.test_engine(request.engine)

    def _emit_libraries(self) -> None:
        """DPIM 关闭时只发空态，不触碰 library data subtree。"""
        manager = self.library_manager
        if manager is None:
            self.emit(LibraryList(libraries=[], current=None, error="小图书馆未启用。"))
            return
        try:
            payload = manager.list_libraries()
            host = self.features.host("dpim") if self.features is not None else None
            state = host.state_payload() if host is not None else {}
            self.emit(
                LibraryList(
                    libraries=payload,
                    current=manager.current_library(),
                    error=str(state.get("error") or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001 - UI 仅收到可读状态
            log.exception("推送书库列表失败")
            self.emit(LibraryList(libraries=[], current=None, error="读取书库列表失败。"))
            self._report("library", ErrorCode.STORAGE_ERROR.value, "读取书库列表失败。", type(exc).__name__)

    def _library_failure(self, exc: Exception, action: str, message: str) -> None:
        """DPIM 管理边界的单点错误映射；不把路径/内容原文回显到错误流。"""
        if type(exc).__name__ == "LibraryDenied":
            self._report("library", ErrorCode.LIBRARY_DENIED.value, str(exc) or message)
        elif isinstance(exc, (ValueError, KeyError)):
            self._report("library", ErrorCode.INVALID_REQUEST.value, str(exc) or message)
        else:
            log.exception("%s 失败", action)
            self._report("library", ErrorCode.STORAGE_ERROR.value, message, type(exc).__name__)

    def _on_library_create(self, request: LibraryCreate) -> None:
        manager = self.library_manager
        if manager is None:
            self._report("library", ErrorCode.INVALID_REQUEST.value, "小图书馆未启用。")
            return
        try:
            library_id = manager.create_library(
                request.name, request.root_kind, request.root, request.group,
                request.model_ref, request.note,
            )
            manager.switch_library(library_id)
        except Exception as exc:  # noqa: BLE001 - 具体归因由统一边界映射
            self._library_failure(exc, "library.create", "创建书库失败。")
        self._emit_libraries()
        if manager.current_library():
            self._on_library_detail(LibraryDetail(id=manager.current_library()))

    def _on_library_update(self, request: LibraryUpdate) -> None:
        manager = self.library_manager
        if manager is None:
            self._report("library", ErrorCode.INVALID_REQUEST.value, "小图书馆未启用。")
            return
        try:
            manager.update_library(
                request.id,
                name=request.name,
                root=request.root,
                group=request.group,
                model_ref=request.model_ref,
                note=request.note,
            )
        except Exception as exc:  # noqa: BLE001
            self._library_failure(exc, "library.update", "保存书库设置失败。")
        self._emit_libraries()

    def _on_library_delete(self, request: LibraryDelete) -> None:
        manager = self.library_manager
        if manager is None:
            self._report("library", ErrorCode.INVALID_REQUEST.value, "小图书馆未启用。")
            return
        try:
            manager.delete_library(request.id)  # 只摘登记，不删任何库文件
        except Exception as exc:  # noqa: BLE001
            self._library_failure(exc, "library.delete", "移除书库登记失败。")
        self._emit_libraries()

    def _on_library_switch(self, request: LibrarySwitch) -> None:
        manager = self.library_manager
        if manager is None:
            self._report("library", ErrorCode.INVALID_REQUEST.value, "小图书馆未启用。")
            return
        try:
            error = manager.switch_library(request.id)
            if error:
                self._report("library", ErrorCode.STORAGE_ERROR.value, "打开书库时检查索引失败。", error)
        except Exception as exc:  # noqa: BLE001
            self._library_failure(exc, "library.switch", "切换书库失败。")
        self._emit_libraries()
        try:
            self._on_library_detail(LibraryDetail(id=request.id))
        except Exception:
            log.exception("切换后读取书库详情失败")

    def _on_library_refresh(self, request: LibraryRefresh) -> None:
        manager = self.library_manager
        if manager is None:
            self._report("library", ErrorCode.INVALID_REQUEST.value, "小图书馆未启用。")
            return
        try:
            manager.refresh_libraries(request.id)
        except Exception as exc:  # noqa: BLE001
            self._library_failure(exc, "library.refresh", "刷新书库索引失败。")
        self._emit_libraries()
        selected = request.id or manager.current_library()
        if selected:
            self._on_library_detail(LibraryDetail(id=selected))

    def _on_library_detail(self, request: LibraryDetail) -> None:
        manager = self.library_manager
        if manager is None:
            self._report("library", ErrorCode.INVALID_REQUEST.value, "小图书馆未启用。")
            return
        try:
            result = manager.library_detail(
                request.id, request.event_offset, request.event_limit, request.graph_limit,
                request.focus_event_id,
            )
            self.emit(LibraryDetailResult(**result))
            graph_data = (
                manager.library_graph(request.graph_library_ids, request.graph_limit)
                if request.graph_library_ids
                else {
                    "library_ids": [request.id],
                    "nodes": result.get("nodes", []),
                    "edges": result.get("edges", []),
                    "truncated": bool(result.get("truncated")),
                }
            )
            self.emit(
                LibraryGraphResult(
                    **graph_data,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._library_failure(exc, "library.detail", "读取书库详情失败。")

    def _on_library_ingest(self, request: LibraryIngest) -> None:
        manager = self.library_manager
        if manager is None:
            self._report("library", ErrorCode.INVALID_REQUEST.value, "小图书馆未启用。")
            return
        try:
            # 新增持久化文本先经通用脱敏；凭据真值不得写入库文件、模型提示词或错误流。
            safe_text = redact(request.text) or ""
            result = manager.ingest_event(
                request.id,
                safe_text,
                request.event_type,
                event_id=request.event_id,
                index=request.index,
            )
            self.emit(LibraryIngestResult(**result))
            self._on_library_detail(LibraryDetail(id=request.id))
        except Exception as exc:  # noqa: BLE001
            self._library_failure(exc, "library.ingest", "保存外部对话失败。")

    def _on_library_query(self, request: LibraryQuery) -> None:
        manager = self.library_manager
        if manager is None:
            self._report("library", ErrorCode.INVALID_REQUEST.value, "小图书馆未启用。")
            return
        try:
            result = manager.query_libraries(
                request.query, request.lib_ids, request.mode, request.top_k
            )
            self.emit(
                LibraryQueryResult(
                    query=result["query"],
                    library_ids=result["library_ids"],
                    results=result["results"],
                    debug=result["debug"],
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._library_failure(exc, "library.query", "书库检索失败。")

    def _on_btcm_update(self, request) -> None:
        host = self.features.host("btcm") if self.features is not None else None
        if host is None:
            self._report("system", ErrorCode.INVALID_REQUEST.value, "副思考链未启用。", None)
            return
        self._persist(
            "btcm.update",
            lambda: host.update(request.trigger, request.slot),
            "更新副思考链设置失败。",
        )
        self._emit_btcm()
        self._emit_health()

    def _on_btcm_run(self, request) -> None:
        """副思考链页「运行一次」：走与 `btcm.think` **相同**的工具路径（不另开通道）。"""
        if self.executor is None:
            return
        host = self.features.host("btcm") if self.features is not None else None
        if host is None:
            self._report("system", ErrorCode.INVALID_REQUEST.value, "副思考链未启用。", None)
            return
        call_id = f"btcm-page-{int(time.time() * 1000)}"
        # 有当前会话则落盘其事件流；无会话时 ctx=None（仅发射事件，不写空 session）。
        ctx = (
            ToolContext(session_id=self.current_session_id, turn_seq=0)
            if self.current_session_id
            else None
        )
        self.executor.execute(
            call_id,
            "btcm.think",
            {"question": request.question, "effort": request.effort, "mode": request.mode},
            ctx,
        )

    def push_initial_state(self) -> None:
        """GUI 连接信号后调用，推送首屏数据。"""
        self._emit_providers()
        self._emit_index()
        self._emit_settings()
        self._emit_health()
        self._emit_personas()
        self._emit_mcp()
        self._emit_skills()
        self._emit_shell()
        self._emit_workspaces()
        self._emit_features()
        self._emit_libraries()
        self._emit_btcm()
        self._emit_builtin()  # v0.0.11（D-1）：插件页「内置工具」分区首屏快照
        self._emit_retrieval()  # rev68：检索页首屏（关档也发空态，页面据 module_state 引导）

    # -- 工作区（v0.0.6） -----------------------------------------------------
    def _session_counts(self) -> dict[str, int]:
        """按工作区统计会话数（含归档）。

        计数在**这里**算而非宿主里：`core.workspace` 不依赖 `core.agent`
        （会话存储），反向依赖就此掐断 —— 宿主只收一张 `{workspace_id: n}` 表。
        """
        counts: dict[str, int] = {}
        try:
            metas = self.store.list(include_archived=True)
        except Exception:  # noqa: BLE001 - 计数失败不该让工作区列表整体失败
            log.exception("统计会话数失败")
            return counts
        for meta in metas:
            key = meta.workspace_id or WS_DEFAULT
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _emit_workspaces(self) -> None:
        """推送工作区快照（登记表 + 当前 + 折叠态）。"""
        if self.workspace_manager is None:
            return
        infos = [
            WorkspaceInfo(**item)
            for item in self.workspace_manager.list_status(self._session_counts())
        ]
        self.emit(
            WorkspaceList(
                workspaces=infos,
                current=self.workspace_manager.current(),
                collapsed=self.workspace_manager.collapsed(),
            )
        )

    def _workspace_failure(self, exc: Exception, action: str, message: str) -> None:
        """工作区操作的失败回报：**按真实原因归码**（一码一义），文案一律可读。"""
        if isinstance(exc, WorkspaceDenied):
            self._report("config", ErrorCode.WORKSPACE_DENIED.value, str(exc) or message)
        elif isinstance(exc, WorkspacePathError):
            self._report("config", ErrorCode.INVALID_REQUEST.value, str(exc) or message)
        elif isinstance(exc, KeyError):
            self._report("config", ErrorCode.INVALID_REQUEST.value, "工作区不存在（可能已被移除）。")
        else:
            log.exception("%s 失败", action)
            self._report("config", ErrorCode.STORAGE_ERROR.value, message, type(exc).__name__)

    def _on_workspace_create(self, request: WorkspaceCreate) -> None:
        if self.workspace_manager is None:
            return
        try:
            workspace_id = self.workspace_manager.create(
                request.name,
                root_kind=request.root_kind,
                root=request.root,
                data_home_kind=request.data_home_kind,
                data_home=request.data_home,
                build_cmd=request.build_cmd,
                note=request.note,
            )
        except Exception as exc:  # noqa: BLE001 - 边界收口，按原因归码
            self._workspace_failure(exc, "workspace.create", "创建工作区失败。")
            self._emit_workspaces()
            return
        # 新建即切为当前：用户刚建工作区，下一步几乎必然是在其中干活
        try:
            self.workspace_manager.switch(workspace_id)
        except KeyError:
            log.warning("新建工作区后切换失败：%s", workspace_id)
        self._emit_workspaces()

    def _on_workspace_update(self, request: WorkspaceUpdate) -> None:
        if self.workspace_manager is None:
            return
        try:
            old_root = self.workspace_manager.root_of(request.id)
        except Exception:
            old_root = None
        try:
            self.workspace_manager.update(
                request.id,
                name=request.name,
                note=request.note,
                build_cmd=request.build_cmd,
                root=request.root,
                data_home_kind=request.data_home_kind,
                data_home=request.data_home,
            )
            new_root = self.workspace_manager.root_of(request.id)
            if old_root is not None and old_root != new_root and self.shell_manager is not None:
                self.shell_manager.close_workspace(request.id)
        except Exception as exc:  # noqa: BLE001
            self._workspace_failure(exc, "workspace.update", "保存工作区失败。")
        self._emit_workspaces()

    def _on_workspace_delete(self, request: WorkspaceDelete) -> None:
        """移除登记 —— **不删磁盘上的任何文件**（宿主层保证，见 §3.13 R2）。

        归属该工作区的会话**回落到默认工作区**（`docs/03` §7「引用即警告」的同族口径：
        删掉被引用的对象时，受影响方回退到默认，而非留下悬空引用）。
        """
        if self.workspace_manager is None:
            return
        try:
            self.workspace_manager.delete(request.id)
        except Exception as exc:  # noqa: BLE001
            self._workspace_failure(exc, "workspace.delete", "移除工作区失败。")
            self._emit_workspaces()
            return
        if self.shell_manager is not None:
            self.shell_manager.close_workspace(request.id)
        self._release_sessions(request.id)
        self._emit_workspaces()
        self._emit_index()  # 归属变了 → 侧栏分组要跟着变

    def _release_sessions(self, workspace_id: str) -> None:
        """把仍指向 `workspace_id` 的会话回落到默认工作区（只改归属，不动事件流）。"""
        try:
            metas = self.store.list(include_archived=True)
        except Exception:  # noqa: BLE001 - 回落失败不该让移除动作整体失败
            log.exception("回落会话归属失败")
            return
        for meta in metas:
            if meta.workspace_id != workspace_id:
                continue
            try:
                self.store.move_session(meta.id, None)
            except KeyError:
                continue

    def _on_workspace_switch(self, request: WorkspaceSwitch) -> None:
        if self.workspace_manager is None:
            return
        try:
            self.workspace_manager.switch(request.id)
        except Exception as exc:  # noqa: BLE001
            self._workspace_failure(exc, "workspace.switch", "切换工作区失败。")
        self._emit_workspaces()

    def _on_workspace_refresh(self, request: WorkspaceRefresh) -> None:
        """重读登记表（文件即配置：手工编辑 `workspaces/index.json` 后刷新即生效）。"""
        if self.workspace_manager is None:
            return
        self.workspace_manager.load()
        self._emit_workspaces()

    def _on_workspace_detail(self, request: WorkspaceDetail) -> None:
        if self.workspace_manager is None:
            return
        try:
            result = self.workspace_manager.detail(request.id, request.path)
        except Exception as exc:  # noqa: BLE001
            self._workspace_failure(exc, "workspace.detail", "读取工作区文件列表失败。")
            return
        self.emit(WorkspaceDetailResult(**result))

    def _on_workspace_memory_write(self, request: WorkspaceMemoryWrite) -> None:
        if self.workspace_manager is None:
            return
        try:
            result = self.workspace_manager.write_memory(
                request.scope, request.workspace_id, request.mode, redact(request.text) or ""
            )
            self.emit(WorkspaceMemoryResult(**result))
        except Exception as exc:  # noqa: BLE001 - 只从显式 UI 请求写入
            self._workspace_failure(exc, "workspace.memory_write", "写入工作区记忆失败。")
            target = request.workspace_id or self.workspace_manager.current()
            self.emit(
                WorkspaceMemoryResult(
                    workspace_id=target,
                    scope=request.scope,
                    mode=request.mode,
                    chars=0,
                    ok=False,
error="写入失败；请检查工作区目录与记忆文件。",
                )
            )

    def _on_workspace_build(self, request: WorkspaceBuild) -> None:
        """运行工作区构建（用户显式通道：逐次点击、命令原文在界面完整展示）。

        不注册为模型工具、不过权限关卡；命令原文只在 `build.json`（用户自己的记录）里，
        审计只记工作区/退出码/耗时。输出的脱敏与截断落在 shell 宿主与工作区宿主两侧。
        """
        if self.workspace_manager is None:
            return
        if self.shell_manager is None:
            self._report("config", ErrorCode.TOOL_UNAVAILABLE.value, "终端功能未启用，无法运行构建。")
            return
        try:
            spec = self.workspace_manager.build_spec(request.id)
        except Exception as exc:  # noqa: BLE001
            self._workspace_failure(exc, "workspace.build", "读取构建配置失败。")
            return
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        try:
            timeout_ms = int(self.config_store.load("settings").workspace.build_timeout_ms)
        except Exception:  # noqa: BLE001 - 配置异常回退出厂默认
            timeout_ms = 600_000
        try:
            outcome = self.shell_manager.run_user_command(
                spec["command"], str(spec["root"]), request.id, timeout_ms
            )
        except Exception as exc:  # noqa: BLE001 - 起壳失败也要给可读回执并落结果
            log.exception("workspace.build 执行失败")
            self._report("config", ErrorCode.STORAGE_ERROR.value, "无法启动构建：请检查 shell 解释器。")
            outcome = {"ok": False, "exit_code": None, "duration_ms": 0,
                       "output": "无法启动构建终端。"}
        log_path = ""
        try:
            written = self.workspace_manager.write_build_result(
                request.id,
                spec["command"],
                started_at,
                outcome.get("exit_code"),
                int(outcome.get("duration_ms") or 0),
                str(outcome.get("output") or ""),
            )
            log_path = written.get("log_path", "")
        except Exception:  # noqa: BLE001 - 落盘失败不吞掉构建结果
            log.exception("写入构建日志失败")
        self.emit(
            WorkspaceBuildResult(
                id=request.id,
                ok=bool(outcome.get("ok")),
                exit_code=outcome.get("exit_code"),
                duration_ms=int(outcome.get("duration_ms") or 0),
                output=str(outcome.get("output") or "")[:250_000],
                log_path=log_path,
                started_at=started_at,
            )
        )

    def _on_workspace_file_read(self, request: WorkspaceFileRead) -> None:
        if self.workspace_manager is None:
            return
        try:
            result = self.workspace_manager.read_file(request.id, request.path)
            self.emit(WorkspaceFileResult(**result))
        except Exception as exc:  # noqa: BLE001 - 只读路径，按原因归码
            self._workspace_failure(exc, "workspace.file_read", "读取文件失败。")
            self.emit(
                WorkspaceFileResult(
                    id=request.id, path=request.path, ok=False, is_write=False,
                    error="无法读取该文件（不存在、非 UTF-8 或超出大小上限）。",
                )
            )

    def _on_workspace_file_write(self, request: WorkspaceFileWrite) -> None:
        if self.workspace_manager is None:
            return
        try:
            result = self.workspace_manager.write_file(
                request.id, request.path, redact(request.content) or "", request.create
            )
            self.emit(WorkspaceFileResult(**result))
        except Exception as exc:  # noqa: BLE001
            self._workspace_failure(exc, "workspace.file_write", "保存文件失败。")
            self.emit(
                WorkspaceFileResult(
                    id=request.id, path=request.path, ok=False, is_write=True,
                    error="保存失败：请检查路径、权限与文件大小。",
                )
            )

    def _on_move_session(self, request: MoveSession) -> None:
        """把会话挪到另一工作区：归属变更只改 meta，事件流不动（`events.jsonl` 只增不改）。"""
        if (
            self.workspace_manager is not None
            and request.workspace_id
            and not self.workspace_manager.exists(request.workspace_id)
        ):
            self._report(
                "config",
                ErrorCode.INVALID_REQUEST.value,
                "目标工作区不存在（可能已被移除）。",
            )
            return
        try:
            previous_workspace = self.store.get_meta(request.session_id).workspace_id
        except KeyError:
            previous_workspace = None
        try:
            self.store.move_session(request.session_id, request.workspace_id)
        except KeyError:
            self._report("session", ErrorCode.SESSION_NOT_FOUND.value, "会话不存在。")
            return
        if previous_workspace != request.workspace_id and self.shell_manager is not None:
            self.shell_manager.close_session(request.session_id)
        self._emit_index()
        self._emit_workspaces()

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

    def _on_mcp_scan(self, request) -> None:
        # v0.0.9：手动安全体检 —— 结果只读展示，不影响权限 / 可见性 / 派发
        if self.mcp_manager is None:
            self._report("system", ErrorCode.INVALID_REQUEST.value, "MCP 模块未启用。", None)
            return
        report = self.mcp_manager.scan_report(request.server_id, request.checks)
        if report is None:
            self._report("config", ErrorCode.INVALID_REQUEST.value, "MCP 服务器不存在。", f"id={request.server_id}")
            return
        self.emit(
            McpScanResult(
                server_id=report["server_id"],
                server_name=report["server_name"],
                scanned_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                findings=report["findings"],
                summary=report["summary"],
            )
        )

    def _retrieval_builtin(self):
        """检索宿主的内置工具面（模块关档即 None，面板不出现 search.* 项）。"""
        if self.features is None:
            return None
        host = self.features.host("retrieval")
        if host is None or not hasattr(host, "KNOWN_BUILTIN_TOOLS"):
            return None
        return host

    def _builtin_items(self) -> list[dict]:
        items: list[dict] = []
        if self.files_manager is not None and hasattr(self.files_manager, "state"):
            items.extend(self.files_manager.state())
        retrieval = self._retrieval_builtin()
        if retrieval is not None:
            items.extend(retrieval.state())
        return items

    def _on_builtin_toggle(self, request) -> None:
        """内置工具开关（v0.0.11 D-1；检索模块轮扩至 search.*）：

        未知名归 tool_unknown，非法值归 tool_invalid_args。先改配置真值
        （plugins.json -> builtin），再刷新工具面（真卸载），最后回推合并快照。
        """
        files_ready = self.files_manager is not None and hasattr(self.files_manager, "toggle_builtin")
        retrieval = self._retrieval_builtin()
        in_files = files_ready and request.name in self.files_manager.KNOWN_BUILTIN_TOOLS
        in_retrieval = retrieval is not None and request.name in retrieval.KNOWN_BUILTIN_TOOLS
        if not in_files and not in_retrieval:
            self._report("config", ErrorCode.TOOL_UNKNOWN.value, "该内置工具不存在。", f"name={request.name}")
            return
        if in_retrieval:
            try:
                retrieval.toggle_builtin(request.name, bool(request.enabled))
            except ValueError as exc:
                self._report("config", ErrorCode.TOOL_INVALID_ARGS.value, str(exc), f"name={request.name}")
                return
        else:
            try:
                self.files_manager.toggle_builtin(request.name, bool(request.enabled))
            except ValueError as exc:
                # 非法值按契约归 invalid_args（一码一义）；未知名已在入口归 tool_unknown
                self._report("config", ErrorCode.TOOL_INVALID_ARGS.value, str(exc), f"name={request.name}")
                return
        self.emit(BuiltinToolState(items=self._builtin_items()))
        try:
            if in_files:
                self.files_manager.refresh()
        except Exception:  # noqa: BLE001 - 刷新失败不计入开关结果
            log.exception("内置工具面刷新失败")

    def _emit_builtin(self) -> None:
        """内置工具快照（D-1；文件工具面存在或检索模块开启时发，避免假空清单）。"""
        files_ready = self.files_manager is not None and hasattr(self.files_manager, "state")
        retrieval = self._retrieval_builtin()
        if not files_ready and retrieval is None:
            return
        self.emit(BuiltinToolState(items=self._builtin_items()))

    def _on_gate_respond(self, request) -> None:
        # 正常路径下 gate.respond 多被 _gate_handler 泵取命中；此处登记以兜底竞态。
        self._gate_decisions[request.call_id] = request.decision == "allow"

    # -- Shell（v0.0.5） -----------------------------------------------------
    def _emit_shell(self) -> None:
        """推送 shell 列表 + 权限档 + 上限（运行态，不入盘）。"""
        if self.shell_manager is None:
            return
        self.emit(
            ShellList(
                shells=self.shell_manager.list_status(),
                max_shells=self.shell_manager.max_shells(),
                permission=self.shell_manager.effective_permission(),
                allow_restricted=self.shell_manager.allow_restricted(),
            )
        )

    def _on_shell_spawn(self, request) -> None:
        if self.shell_manager is None:
            return
        workspace_id = None
        workspace_root = None
        if self.workspace_manager is not None:
            try:
                workspace_id = self.workspace_manager.current()
                workspace_root = str(self.workspace_manager.root_of(workspace_id))
            except Exception:
                log.exception("解析当前工作区 root 失败，shell 将按配置目录启动")
        created = self.shell_manager.spawn(
            cwd=request.cwd,
            session_id=self.current_session_id or "",
            workspace_id=workspace_id,
            workspace_root=workspace_root,
        )
        if created is None:
            # 起不来必须有可读回执（用户点按钮却毫无反应是最差体验）。
            self._report(
                "system",
                ErrorCode.TOOL_UNAVAILABLE.value,
                self.shell_manager.last_error() or "无法创建终端。",
            )
        self._emit_shell()

    def _on_shell_close(self, request) -> None:
        if self.shell_manager is not None:
            self.shell_manager.close(request.id)
            self._emit_shell()

    def _on_shell_input(self, request) -> None:
        if self.shell_manager is not None:
            self.shell_manager.input(request.id, request.command)
            self._emit_shell()

    def _on_shell_refresh(self, request) -> None:
        """刷新 = 重读配置再回推：`modules.json → shell` 的权限档/上限/高危开关改完即生效。

        与 docs 01 §7.3「文件即配置」一致；不新造运行期提权通道（提权仍是改文件 + 显式动作）。
        """
        if self.shell_manager is None:
            return
        self.shell_manager.load()
        self.refresh_modules()
        self._emit_shell()

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
                    deferred.append(request)  # 他方关卡的裁决：不吞，回投待正常分派兜底登记
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
        # v0.0.11：文件工具的 precheck 拿不到 ctx，故在每次请求分发前同步「当前会话」
        # （工具调用只发生在 msg.user 之内，此处同步足够；换会话会清空读记账）。
        if self.files_manager is not None:
            self.files_manager.set_active_session(self.current_session_id)
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
        elif t == "provider.toggle":
            self._on_provider_toggle(request)
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
        elif t == "mcp.scan":
            self._on_mcp_scan(request)
        elif t == "tool.builtin.toggle":
            self._on_builtin_toggle(request)
        elif t == "tool.builtin.refresh":
            self._emit_builtin()
        elif t == "shell.spawn":
            self._on_shell_spawn(request)
        elif t == "shell.close":
            self._on_shell_close(request)
        elif t == "shell.input":
            self._on_shell_input(request)
        elif t == "shell.refresh":
            self._on_shell_refresh(request)
        elif t == "workspace.create":
            self._on_workspace_create(request)
        elif t == "workspace.update":
            self._on_workspace_update(request)
        elif t == "workspace.delete":
            self._on_workspace_delete(request)
        elif t == "workspace.switch":
            self._on_workspace_switch(request)
        elif t == "workspace.refresh":
            self._on_workspace_refresh(request)
        elif t == "workspace.detail":
            self._on_workspace_detail(request)
        elif t == "workspace.memory_write":
            self._on_workspace_memory_write(request)
        elif t == "workspace.build":
            self._on_workspace_build(request)
        elif t == "workspace.file_read":
            self._on_workspace_file_read(request)
        elif t == "workspace.file_write":
            self._on_workspace_file_write(request)
        elif t == "session.move":
            self._on_move_session(request)
        elif t == "library.create":
            self._on_library_create(request)
        elif t == "library.update":
            self._on_library_update(request)
        elif t == "library.delete":
            self._on_library_delete(request)
        elif t == "library.switch":
            self._on_library_switch(request)
        elif t == "library.refresh":
            self._on_library_refresh(request)
        elif t == "library.detail":
            self._on_library_detail(request)
        elif t == "library.ingest":
            self._on_library_ingest(request)
        elif t == "library.query":
            self._on_library_query(request)
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
        elif t == "feature.toggle":
            self._on_feature_toggle(request)
        elif t == "btcm.update":
            self._on_btcm_update(request)
        elif t == "btcm.run":
            self._on_btcm_run(request)
        elif t == "retrieval.refresh":
            self._on_retrieval_refresh(request)
        elif t == "retrieval.config.update":
            self._on_retrieval_config(request)
        elif t == "retrieval.key.set":
            self._on_retrieval_key(request)
        elif t == "retrieval.test":
            self._on_retrieval_test(request)
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
            meta = self.store.create(
                None, persona_id, workspace_id=self._current_workspace()
            )
            self.current_session_id = meta.id
            self.emit(SessionCreated(session_id=meta.id, title=meta.title, created_at=meta.created_at))
            self._emit_index()
            self._emit_personas()
            self._emit_workspaces()
        return self.current_session_id

    def _current_workspace(self) -> str | None:
        """新会话默认归属的工作区。**默认工作区记 `None`** —— 与存量会话（缺字段）同态，
        避免「新会话记 ws_default、老会话记 None」两套表示法（`docs/03` §7 引用即警告的反面）。"""
        if self.workspace_manager is None:
            return None
        current = self.workspace_manager.current()
        return None if current == WS_DEFAULT else current

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
        # v0.0.6：显式指定优先，否则进**当前工作区**（默认工作区记 None）
        workspace_id = request.workspace_id or self._current_workspace()
        meta = self.store.create(request.title, persona_id, workspace_id=workspace_id)
        self.current_session_id = meta.id
        self.emit(SessionCreated(session_id=meta.id, title=meta.title, created_at=meta.created_at))
        self._emit_index()
        self._emit_personas()  # 新会话的 in_session 标记变了
        self._emit_workspaces()  # 侧栏分组计数变了
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
        with contextlib.suppress(KeyError):  # 已不存在：静默幂等
            self.store.archive(request.session_id)
        self._emit_index()

    def _on_unarchive(self, request: UnarchiveSession) -> None:
        with contextlib.suppress(KeyError):
            self.store.unarchive(request.session_id)
        self._emit_index()

    def _on_rename(self, request: RenameSession) -> None:
        with contextlib.suppress(KeyError):
            self.store.rename(request.session_id, request.title)
        self._emit_index()

    def _on_delete(self, request: DeleteSession) -> None:
        self.store.delete(request.session_id)
        if self.current_session_id == request.session_id:
            self.current_session_id = None
        # v0.0.5：会话删除 → 关闭其派生的 shell（不留悬挂进程；docs 03 §8 运行态不入盘）。
        if self.shell_manager is not None:
            self.shell_manager.close_session(request.session_id)
            self._emit_shell()
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

    def _on_provider_toggle(self, request: ProviderToggle) -> None:
        """供应商启停（rev68；用户裁决）：禁用 = 其模型不再可解析，槽位保留可逆。"""
        self._persist(
            "供应商启停",
            lambda: self.gateway.toggle_provider(request.provider_id, request.enabled),
            "供应商启停失败：请检查数据目录是否可写。",
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
        if request.section == "ui":
            # v0.0.6：折叠态住在 settings.ui —— 落盘后要把工作区快照重发一次，
            # 否则界面上的 `collapsed` 会停在旧值（单一来源在配置，不在界面缓存）。
            self._emit_workspaces()

    # -- 退出收口 ----------------------------------------------------------
    def shutdown(self) -> None:
        """结束当前会话并把事件流 fsync 到磁盘（docs 03 §10）。

        由应用入口在**核心线程停止之后**调用，故此处不存在并发写；
        退出路径不得再向外抛异常。
        """
        session_id = self.current_session_id
        # 切片 0：附加功能统一收尾（MCP 连接 / shell 子进程等，退出路径不抛）。
        if self.features is not None:
            try:
                self.features.shutdown()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                log.exception("附加功能收尾失败")
        if not session_id:
            return
        try:
            self.store.end(session_id, "app_exit")
        except Exception:  # noqa: BLE001 - 退出路径不抛
            log.exception("会话收尾失败")
