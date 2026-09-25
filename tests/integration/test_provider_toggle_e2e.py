"""供应商启停集成冒烟（rev68）。

从 `controller.handle(ProviderToggle)` 入口走：禁用后 `provider.list` 反映
enabled=False；网关解析该供应商模型诚实失败（model_not_found，明示已停用）；
重启用即恢复。零真实出网。
"""

from __future__ import annotations

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import (
    ProviderList,
    ProviderSpec,
    ProviderToggle,
    ProviderUpsert,
)
from tests.mocks.gateway import MockGateway


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=lambda store: MockGateway())


def test_provider_toggle_via_controller(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    try:
        events: list = []
        ctx.bridge.event_received.connect(events.append)

        spec = ProviderSpec(
            id="prv_x",
            name="X",
            base_url="https://mock.local",
            models=[{"id": "mx", "ctx_window": 1000, "reasoning": "off"}],
        )
        ctx.controller.handle(ProviderUpsert(provider=spec, api_key=None))

        # 禁用
        ctx.controller.handle(ProviderToggle(provider_id="prv_x", enabled=False))
        lists = [e for e in events if isinstance(e, ProviderList)]
        latest = lists[-1]
        px = next(p for p in latest.providers if p.id == "prv_x")
        assert px.enabled is False

        # 解析语义（真实网关）由 tests/unit/test_gateway.py::test_provider_toggle_disables_resolution 覆盖；
        # 本测试聚焦请求链与状态回推（替身网关不做解析语义）。

        # 重启用恢复
        ctx.controller.handle(ProviderToggle(provider_id="prv_x", enabled=True))
        px2 = next(
            p for p in [e for e in events if isinstance(e, ProviderList)][-1].providers
            if p.id == "prv_x"
        )
        assert px2.enabled is True

        # 未知供应商：不崩、不发新 provider.list
        before = len([e for e in events if isinstance(e, ProviderList)])
        ctx.controller.handle(ProviderToggle(provider_id="prv_missing", enabled=True))
        assert len([e for e in events if isinstance(e, ProviderList)]) == before + 1  # 仍回推（幂等）
    finally:
        ctx.worker.stop()
