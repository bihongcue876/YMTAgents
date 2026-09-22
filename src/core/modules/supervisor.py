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
    def set_state(self, module: str, state: ModuleState | str) -> None: ...

    @abstractmethod
    def reload(self, module: str) -> None: ...


class ModuleSupervisor(IModuleSupervisor):
    def __init__(self) -> None:
        # 首期仅 mcp 有宿主实现（rev41/rev44）；其余仍 disabled。
        self._states: dict[str, str] = dict.fromkeys(MODULE_NAMES, ModuleState.DISABLED.value)

    def get_states(self) -> dict[str, str]:
        return dict(self._states)

    def set_state(self, module: str, state: ModuleState | str) -> None:
        if module in self._states:
            self._states[module] = state.value if isinstance(state, ModuleState) else str(state)

    def reload(self, module: str) -> None:
        # 宿主模块的启停由各自 manager 负责；supervisor 只聚合健康度。
        return None
