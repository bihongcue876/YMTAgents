"""附加功能宿主协议（切片 0）。

「附加功能」= 可整体启停的增强器官（MCP / Shell / Skills / BTCM / DPIM）。
本协议只约定生命周期，不关心实现：

- `activate`：装配并注册（惰性；关档时其模块**不被 import**）。
- `deactivate`：**真卸载** —— 注销工具、停掉后台（子进程等）、断开引用。
- `host_state`：宿主态（`ready`/`degraded`/`error`/`disabled`），供 supervisor 与界面。
- `state_payload`：开启后的**子选项**态（如 btcm 的手动/自动与槽位）；无子选项者返回空表。

六条硬指标（关档时必须全部成立）：不 import / 不注册 / 无后台活动 / 释放引用 /
状态可见 / 可回切。见 spec `spec-2026-09-23-btcm.md` 与 `spec-2026-09-23-dpim.md`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable


class IFeatureHost(ABC):
    @abstractmethod
    def activate(self) -> None: ...

    @abstractmethod
    def deactivate(self) -> None: ...

    @abstractmethod
    def host_state(self) -> str: ...

    def state_payload(self) -> dict:
        """子选项态（默认无子选项）。实现方可覆盖，供界面回推。"""
        return {}


class IFeatureManager(ABC):
    """附加功能生命周期管理器（宿主启停的唯一入口）。

    分工：`ModuleSupervisor` 只**聚合展示**健康度；真正的启停决策与装配/卸载在这里。
    """

    @abstractmethod
    def register(self, name: str, factory: Callable[[], IFeatureHost]) -> None:
        """登记工厂（工厂体内局部 import，保证关档不 import）。"""

    @abstractmethod
    def names(self) -> list[str]: ...

    @abstractmethod
    def enabled(self, name: str) -> bool:
        """配置真值（`modules.json → features`）。"""

    @abstractmethod
    def load(self) -> None:
        """按配置装配全部已启用功能（启动路径）。"""

    @abstractmethod
    def activate(self, name: str) -> bool:
        """惰性装配；失败返回 False（不得阻断启动）。"""

    @abstractmethod
    def deactivate(self, name: str) -> bool:
        """真卸载：宿主收尾 → 摘引用 → 回收。"""

    @abstractmethod
    def toggle(self, name: str, enabled: bool) -> bool:
        """落盘真值 + 启停；装配失败时**回退真值**并返回 False。"""

    @abstractmethod
    def host(self, name: str) -> IFeatureHost | None:
        """实时宿主；关档/未装配即 None。"""

    @abstractmethod
    def states(self) -> list[dict]:
        """`[{name, enabled, state, available}]`，供界面与 supervisor。

        `available=False` 表示配置已声明但当前没有已登记宿主工厂；界面必须禁用其开关。
        """

    @abstractmethod
    def shutdown(self) -> None: ...
