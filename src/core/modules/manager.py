"""附加功能生命周期（切片 0）。

宿主由**工厂惰性构造**：关档不仅不注册，连模块都不 import（六条硬指标之一）。
`deactivate` 走「宿主真卸载 → 断开引用 → gc」；配置真值在 `modules.json` 的 `features` 段。

与 `ModuleSupervisor` 的分工：supervisor 只**聚合展示**各模块健康度；真正的启停决策
与装配/卸载在这里。附加功能比模块多（含 skills），故两者不共名单。
"""

from __future__ import annotations

import gc
import logging
from collections.abc import Callable
from typing import Any

from core.modules.feature import IFeatureHost, IFeatureManager

log = logging.getLogger(__name__)


class FeatureManager(IFeatureManager):
    def __init__(
        self,
        config_store: Any,
        audit: Callable[..., None] | None = None,
        emit: Callable[[Any], None] | None = None,
    ) -> None:
        self._config_store = config_store
        self._audit = audit or (lambda *_a, **_k: None)
        self._emit = emit or (lambda *_a, **_k: None)
        self._factories: dict[str, Callable[[], IFeatureHost]] = {}
        self._hosts: dict[str, IFeatureHost] = {}

    # -- 注册 --------------------------------------------------------------
    def register(self, name: str, factory: Callable[[], IFeatureHost]) -> None:
        """登记一个附加功能工厂。工厂体内**局部 import**，保证关档不 import。"""
        self._factories[name] = factory

    def names(self) -> list[str]:
        return list(self._factories)

    # -- 配置真值（modules.json → features）--------------------------------
    def _features(self):
        return self._config_store.load("modules").features

    def enabled(self, name: str) -> bool:
        return bool(getattr(self._features(), name, False))

    def _set_enabled(self, name: str, value: bool) -> None:
        modules = self._config_store.load("modules")
        setattr(modules.features, name, bool(value))
        self._config_store.save("modules", modules)

    # -- 生命周期 ----------------------------------------------------------
    def load(self) -> None:
        """按配置装配全部已启用功能（启动路径）。"""
        for name in self.names():
            if self.enabled(name):
                self.activate(name)

    def activate(self, name: str) -> bool:
        if name in self._hosts:
            return True
        factory = self._factories.get(name)
        if factory is None:
            return False
        host: IFeatureHost | None = None
        try:
            host = factory()
            host.activate()
        except Exception:
            log.exception("附加功能装配失败：%s", name)
            if host is not None:
                try:
                    host.deactivate()
                except Exception:
                    log.exception("装配失败后的附加功能清理失败：%s", name)
            self._audit("feature.activate", name=name, ok=False)
            return False
        self._hosts[name] = host
        self._audit("feature.activate", name=name, ok=True)
        return True

    def deactivate(self, name: str) -> bool:
        """真卸载：宿主先自行收尾（注销工具/停后台），随后断开引用并回收。"""
        host = self._hosts.pop(name, None)
        if host is None:
            return False
        try:
            host.deactivate()
        except Exception:
            log.exception("附加功能卸载失败：%s", name)
        del host
        gc.collect()
        self._audit("feature.deactivate", name=name, ok=True)
        return True

    def toggle(self, name: str, enabled: bool) -> bool:
        """落盘真值 + 启停；**开档装配失败即回退真值**（避免「配置为开、宿主不存在」）。

        返回 False 只表示「未能到达请求的档位」，真值已回到与实际一致的一侧。
        """
        if name not in self._factories:
            raise ValueError(f"未知附加功能：{name}")
        if not enabled:
            self._set_enabled(name, False)
            self.deactivate(name)
            return True
        # 先落盘：宿主装配时经 `config_store.load("modules")` 读真值。
        self._set_enabled(name, True)
        if self.activate(name):
            return True
        self._set_enabled(name, False)
        return False

    def host(self, name: str) -> IFeatureHost | None:
        return self._hosts.get(name)

    def states(self) -> list[dict]:
        """列出配置声明的全部插入式模块及当前可用性。

        未登记工厂的模块保留在界面清单中，但标为 unavailable；这只读取共享配置
        schema，不会 import 或访问对应模块实现。这样 UI 可以诚实展示尚未接入项，
        又不会把它伪装成可启动宿主。
        """
        features = self._features()
        out: list[dict] = []
        for name in type(features).model_fields:
            available = name in self._factories
            host = self._hosts.get(name)
            if not available:
                state = "unavailable"
            elif host is None:
                state = "disabled"
            else:
                try:
                    state = host.host_state()
                except Exception:  # noqa: BLE001 - 状态查询异常不得外溢
                    state = "error"
            out.append(
                {
                    "name": name,
                    "enabled": bool(getattr(features, name, False)),
                    "state": state,
                    "available": available,
                }
            )
        return out

    def shutdown(self) -> None:
        for name in list(self._hosts):
            self.deactivate(name)
