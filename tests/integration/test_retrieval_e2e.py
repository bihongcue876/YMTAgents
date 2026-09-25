"""检索模块集成冒烟（spec-2026-09-25-retrieval §13）。

按铁律从 `controller.handle(Request)` 入口走：模块关档 → invalid_request；
开档 → config.update / key.set / test 全链。零真实出网（mock open_bounded），
密钥用替身（不经 DPAPI）。
"""

from __future__ import annotations

import core.retrieval.engines as eng
from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import (
    ErrorReport,
    RetrievalRefresh,
    RetrievalConfigUpdate,
    RetrievalKeySet,
    RetrievalState,
    RetrievalTest,
    RetrievalTestResult,
)
from tests.mocks.gateway import MockGateway

DDG_HTML = (
    '<a class="result__a" href="https://a.example/x">标题A</a>'
    '<a class="result__snippet" href="#">摘要A</a>'
)


class FakeSecrets:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, name, value):
        self.store[name] = value

    def get(self, name):
        return self.store.get(name)

    def delete(self, name):
        self.store.pop(name, None)

    def status(self, name):
        return "stored" if name in self.store else "missing"


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(
        gateway_factory=lambda store: MockGateway(), secrets=FakeSecrets()
    )


def test_retrieval_off_reports_invalid_request(tmp_path, monkeypatch) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    try:
        events: list = []
        original = ctx.controller.emit
        ctx.controller.emit = lambda event: (events.append(event), original(event))[1]

        ctx.controller.handle(RetrievalRefresh())

        errors = [e for e in events if isinstance(e, ErrorReport)]
        assert len(errors) == 1
        assert errors[0].code == "invalid_request"
        assert "检索模块未启用" in errors[0].message
    finally:
        ctx.worker.stop()


def test_retrieval_full_chain_via_controller(tmp_path, monkeypatch) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    try:
        events: list = []
        ctx.bridge.event_received.connect(events.append)

        # 开模块（feature.toggle 经 FeatureManager 落盘 + 惰性装配）
        from shared.envelope import FeatureToggle

        ctx.controller.handle(FeatureToggle(name="retrieval", enabled=True))

        # 启用引擎：白名单自动添加 + state 回推
        ctx.controller.handle(RetrievalConfigUpdate(engines={"duckduckgo": True}))

        states = [e for e in events if isinstance(e, RetrievalState)]
        assert len(states) >= 2  # 首帧（开模块）+ config.update
        latest = states[-1]
        ddg = next(item for item in latest.engines if item["name"] == "duckduckgo")
        assert ddg["enabled"] is True
        assert ddg["key_state"] == "missing"
        assert ddg["endpoints"] == ["html.duckduckgo.com"]

        settings = ctx.config_store.load("settings")
        assert "html.duckduckgo.com" in settings.network.whitelist  # 白名单自动添加（W1）

        # key 写入 → vault 命名 + 状态徽标（值不回推）
        ctx.controller.handle(RetrievalKeySet(engine="exa", value="sk-test"))
        assert ctx.secrets.store.get("api_key/retrieval.exa") == "sk-test"
        states2 = [e for e in events if isinstance(e, RetrievalState)]
        exa = next(item for item in states2[-1].engines if item["name"] == "exa")
        assert exa["key_state"] == "stored"
        assert all("sk-test" not in repr(e.model_dump()) for e in events)

        # 清除
        ctx.controller.handle(RetrievalKeySet(engine="exa", value=None))
        assert "api_key/retrieval.exa" not in ctx.secrets.store

        # 单引擎测试（mock 出网）：免 key 引擎 R2 随启用自动通过
        monkeypatch.setattr(
            eng, "open_bounded", lambda req, timeout: (DDG_HTML, None)
        )
        ctx.controller.handle(RetrievalTest(engine="duckduckgo"))
        from shared.envelope import RetrievalTestResult

        results = [e for e in events if isinstance(e, RetrievalTestResult)]
        assert len(results) == 1
        assert results[0].engine == "duckduckgo"
        r2 = next(f for f in results[0].findings if f["id"] == "R2")
        assert r2["status"] == "pass"
        r4 = next(f for f in results[0].findings if f["id"] == "R4")
        assert r4["status"] == "skip"

        # refresh 请求：重发 state
        before = len([e for e in events if isinstance(e, RetrievalState)])
        ctx.controller.handle(RetrievalRefresh())
        assert len([e for e in events if isinstance(e, RetrievalState)]) == before + 1
    finally:
        ctx.worker.stop()
