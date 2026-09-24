"""启动装配（docs 04 §6）。

8 阶段：路径 → 迁移 → 配置装载 → 边界关卡/落盘 → 注册(supervisor/registry)
→ 网关就绪 → Agent 待命 → GUI 就绪（GUI 由 main 装配）。
阶段 5–7 的任何故障不得阻止应用进入可对话状态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app import logging_setup, paths
from app.controller import CoreController
from app.core_thread import CoreWorker
from core.agent.loop import AgentLoop
from core.agent.persona import PersonaStore
from core.agent.session import SessionStore
from core.bus.bridge import BusBridge
from core.bus.sink import EventSink
from core.gateway.provider import ModelGateway
from core.modules.manager import FeatureManager
from core.modules.supervisor import ModuleSupervisor
from core.registry.executor import ToolExecutor
from core.registry.registry import Registry
from core.security.dpapi import DpapiBox
from core.security.legacy import LegacyKeyring, migrate_keyring_to_vault
from core.security.vault import ISecretStore, Vault
from core.store.config_store import ConfigStore
from core.store.migrate import migrate_all
from core.workspace.manager import WorkspaceManager

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    root: Path
    bridge: BusBridge
    config_store: ConfigStore
    session_store: SessionStore
    gateway: ModelGateway
    supervisor: ModuleSupervisor
    registry: Registry
    agent: AgentLoop
    controller: CoreController
    worker: CoreWorker
    personas: PersonaStore
    secrets: ISecretStore
    #: 附加功能生命周期（切片 0）：宿主启停的唯一真值 + 惰性装配/真卸载。
    features: FeatureManager
    #: 以下为**初始快照**（便利字段，供集成测试/旧调用读取）；运行期启停请走 `features.host()`。
    mcp_manager: object | None
    executor: ToolExecutor
    skill_manager: object | None
    shell_manager: object | None
    workspace_manager: WorkspaceManager


def bootstrap(
    gateway_factory: Callable[[ConfigStore], ModelGateway] | None = None,
    secrets: ISecretStore | None = None,
    legacy: LegacyKeyring | None = None,
) -> AppContext:
    root = paths.ensure_skeleton()
    migrate_all(root)

    config_store = ConfigStore(root)
    # 日志装配：级别取自 settings.logging.level（此前该设置从不生效），
    # 文件落 <数据根>/logs/app.log，并挂脱敏过滤器（rev9 §3/§4）。
    logging_setup.configure(root, config_store.load("settings").logging.level)

    bridge = BusBridge()
    sink = EventSink(root)
    session_store = SessionStore(root, sink)

    # v0.0.2：机密库 + 旧密钥迁移（配置装载后、网关构造前；失败不阻断启动）。
    secret_store = secrets or Vault(root, DpapiBox(), audit=sink.append_audit)
    migrate_keyring_to_vault(config_store, secret_store, legacy, audit=sink.append_audit)

    if gateway_factory:
        gateway = gateway_factory(config_store)
    else:
        gateway = ModelGateway(config_store, secret_store)
    supervisor = ModuleSupervisor()
    registry = Registry()
    personas = PersonaStore(root / "personas")
    # v0.0.6：工作区宿主。启动成本 = 读一次 index.json（缺失则生成默认工作区），
    # 零子进程、零网络；登记表不可读时按空表加载并留可读提示（不阻断启动）。
    workspace_manager = WorkspaceManager(root, config_store, audit=sink.append_audit)
    workspace_manager.load()
    # 切片 0：附加功能生命周期。宿主由工厂**惰性构造**（关档不 import）；
    # 仅装配 `modules.json → features` 中已启用者（默认 mcp/shell/skills 开，btcm/dpim 关）。
    features = FeatureManager(config_store, audit=sink.append_audit, emit=bridge.emit_event)

    def _mcp_factory():
        from core.mcp.manager import McpManager

        return McpManager(
            registry, config_store, secret_store, audit=sink.append_audit, emit=bridge.emit_event
        )

    def _shell_factory():
        from core.shell.manager import ShellManager

        # 启动零子进程：activate 只读配置 + 探测解释器（which），首次 shell.exec 才 spawn。
        def _session_workspace(session_id: str) -> tuple[str | None, str | None]:
            try:
                meta = session_store.get_meta(session_id)
                workspace_id = meta.workspace_id or "ws_default"
                return workspace_id, str(workspace_manager.root_of(meta.workspace_id))
            except Exception:  # noqa: BLE001 - 无效/旧会话回退 shell.cwd 配置
                return None, None

        return ShellManager(
            registry, config_store, audit=sink.append_audit, emit=bridge.emit_event,
            resolve_workspace=_session_workspace,
        )

    def _skills_factory():
        from core.skills.manager import SkillManager

        return SkillManager(
            config_store,
            registry,
            root / "skills",
            audit=sink.append_audit,
            presets_dir=Path(__file__).resolve().parent.parent / "core" / "skills" / "presets",
        )

    def _btcm_factory():
        from core.modules.btcm.manager import BtcmManager

        def _session_model(sid: str) -> str | None:
            return session_store.get_meta(sid).main_model

        return BtcmManager(
            registry,
            config_store,
            gateway,
            emit=bridge.emit_event,
            audit=sink.append_audit,
            resolve_session_model=_session_model,
        )

    def _dpim_factory():
        from core.modules.dpim.manager import DpimManager

        return DpimManager(
            registry,
            root,
            gateway,
            config_store,
            audit=sink.append_audit,
        )

    features.register("mcp", _mcp_factory)
    features.register("shell", _shell_factory)
    features.register("skills", _skills_factory)
    features.register("btcm", _btcm_factory)
    features.register("dpim", _dpim_factory)
    features.load()

    executor = ToolExecutor(
        registry, store=session_store, emit=bridge.emit_event, audit=sink.append_audit
    )

    def _workspace_memory_paths(session_id: str) -> list[Path]:
        meta = session_store.get_meta(session_id)
        return workspace_manager.cascade_dirs(meta.workspace_id)

    def _workspace_root(session_id: str) -> Path:
        meta = session_store.get_meta(session_id)
        return workspace_manager.root_of(meta.workspace_id)

    agent = AgentLoop(
        session_store, gateway, bridge.emit_event, root, config_store,
        personas=personas, executor=executor, workspace_memory_paths=_workspace_memory_paths,
        workspace_root=_workspace_root,
    )
    controller = CoreController(
        bridge, session_store, gateway, agent, supervisor, registry, config_store, root,
        personas=personas, executor=executor, features=features,
        workspace_manager=workspace_manager,
    )
    controller.refresh_modules()
    worker = CoreWorker(bridge, controller.handle, on_stop=features.shutdown)
    worker.start()

    return AppContext(
        root=root,
        bridge=bridge,
        config_store=config_store,
        session_store=session_store,
        gateway=gateway,
        supervisor=supervisor,
        registry=registry,
        agent=agent,
        controller=controller,
        worker=worker,
        personas=personas,
        secrets=secret_store,
        features=features,
        mcp_manager=features.host("mcp"),
        executor=executor,
        skill_manager=features.host("skills"),
        shell_manager=features.host("shell"),
        workspace_manager=workspace_manager,
    )
