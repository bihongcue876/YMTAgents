"""模块生命周期 supervisor（spec §2.6 / docs 04 §4）。

首期四个模块宿主全部 disabled；supervisor 仍聚合健康度上报（GUI 首期不渲染）。
disabled 模块不 import、不实例化、不读其数据子树（隔离铁律）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from shared.enums import ModuleState

MODULE_NAMES = ("dpim", "btcm", "mcp", "shell")


class IModuleSupervisor(ABC):
    @abstractmethod
    def get_states(self) -> dict[str, str]: ...

    @abstractmethod
    def reload(self, module: str) -> None: ...


class ModuleSupervisor(IModuleSupervisor):
    def get_states(self) -> dict[str, str]:
        return {name: ModuleState.DISABLED.value for name in MODULE_NAMES}

    def reload(self, module: str) -> None:
        # 首期无模块实现（占位）：无操作。
        return None
