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
from core.agent.session import SessionStore
from core.bus.bridge import BusBridge
from core.bus.sink import EventSink
from core.gateway.provider import ModelGateway
from core.modules.supervisor import ModuleSupervisor
from core.registry.registry import Registry
from core.store.config_store import ConfigStore
from core.store.migrate import migrate_all

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


def bootstrap(gateway_factory: Callable[[ConfigStore], ModelGateway] | None = None) -> AppContext:
    root = paths.ensure_skeleton()
    migrate_all(root)

    config_store = ConfigStore(root)
    # 日志装配：级别取自 settings.logging.level（此前该设置从不生效），
    # 文件落 <数据根>/logs/app.log，并挂脱敏过滤器（rev9 §3/§4）。
    logging_setup.configure(root, config_store.load("settings").logging.level)

    bridge = BusBridge()
    sink = EventSink(root)
    session_store = SessionStore(root, sink)

    gateway = gateway_factory(config_store) if gateway_factory else ModelGateway(config_store)
    supervisor = ModuleSupervisor()
    registry = Registry()
    agent = AgentLoop(session_store, gateway, bridge.emit_event, root, config_store)
    controller = CoreController(
        bridge, session_store, gateway, agent, supervisor, registry, config_store, root
    )
    worker = CoreWorker(bridge, controller.handle)
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
    )
