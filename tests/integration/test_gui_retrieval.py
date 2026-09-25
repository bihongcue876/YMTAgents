"""检索页 GUI 集成冒烟（rev68；用户裁决：独立一页）。

覆盖：模块关档引导态；开档后工具/引擎两组全开关、状态回推；引擎开关经 bus 提交
`retrieval.config.update`；工具开关提交 `tool.builtin.toggle`；测试按钮提交
`retrieval.test` 并回显计数。零真实出网（密钥替身）。
"""

from __future__ import annotations

import time

from PySide6.QtWidgets import QLabel

from app import bootstrap as bootstrap_mod
from app import paths
from gui.main_window import MainWindow
from shared.envelope import (
    BuiltinToolToggle,
    FeatureToggle,
    RetrievalConfigUpdate,
    RetrievalKeySet,
    RetrievalState,
    RetrievalTest,
)
from tests.mocks.gateway import MockGateway


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


def _window(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    ctx = bootstrap_mod.bootstrap(
        gateway_factory=lambda store: MockGateway(), secrets=FakeSecrets()
    )
    return ctx, MainWindow(ctx.bridge, data_root=str(ctx.root))


def _pump(qapp, seconds: float = 0.3) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)


def test_retrieval_page_disabled_and_enabled_states(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        ctx.controller.push_initial_state()
        qapp.processEvents()

        page = window.retrieval_page
        # 关档：引导态（无行控件）
        _pump(qapp, 0.3)
        assert "未启用" in page._note.text()

        # 开模块（附加功能总开关）→ 页面出现两组行：工具 2 + 引擎 6（webfetch 不单列）
        ctx.controller.handle(FeatureToggle(name="retrieval", enabled=True))
        _pump(qapp)

        from gui.widgets.switch import Switch

        switches = page.findChildren(Switch)
        assert len(switches) == 8
        # 工具默认启用（与 file.* 同约定：builtin 缺省 enabled）；引擎默认全关
        assert page._tool_rows["search.web"]["switch"].isChecked() is True
        assert all(
            not page._engine_rows[name]["switch"].isChecked()
            for name in page._engine_rows
        )

        # 状态徽标：key 缺失（免费引擎不走 vault）
        assert "无 key" in page._engine_rows["duckduckgo"]["badge"].text()

        # key 保存 → vault 命名 + 徽标回推（值不回显）
        ctx.controller.handle(RetrievalKeySet(engine="exa", value="sk-secret"))
        _pump(qapp)
        assert ctx.secrets.store.get("api_key/retrieval.exa") == "sk-secret"
        assert "已存" in page._engine_rows["exa"]["badge"].text()
        _pump(qapp, 0.1)
        # 值永不出现在任何 GUI 可见状态
        assert "sk-secret" not in page._note.text()
    finally:
        ctx.worker.stop()
        window.close()


def test_retrieval_page_switches_submit_requests(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        ctx.controller.push_initial_state()
        qapp.processEvents()

        # 捕获 bus 提交（GUI -> core 请求面）
        submitted: list = []
        original = window.bus.submit
        window.bus.submit = lambda request: (submitted.append(request), original(request))[1]

        ctx.controller.handle(FeatureToggle(name="retrieval", enabled=True))
        _pump(qapp)

        page = window.retrieval_page
        page._engine_rows["duckduckgo"]["switch"].setChecked(True)
        _pump(qapp, 0.5)
        updates = [r for r in submitted if isinstance(r, RetrievalConfigUpdate)]
        assert updates and updates[-1].engines == {"duckduckgo": True}

        page._tool_rows["search.fetch"]["switch"].setChecked(False)  # 默认启用 -> 关（状态变化触发信号）
        _pump(qapp, 0.5)
        assert any(isinstance(r, BuiltinToolToggle) and r.name == "search.fetch" and not r.enabled for r in submitted)

        page.test_requested.emit("arxiv")
        _pump(qapp, 0.5)
        assert any(isinstance(r, RetrievalTest) and r.engine == "arxiv" for r in submitted)

        page.refresh_requested.emit()
        _pump(qapp, 0.3)
        assert any(type(r).__name__ == "RetrievalRefresh" for r in submitted)

        # key 保存信号经 bus
        page.key_set.emit("exa", "sk-new")
        _pump(qapp, 0.3)
        assert any(isinstance(r, RetrievalKeySet) and r.value == "sk-new" for r in submitted)
    finally:
        ctx.worker.stop()
        window.close()


def test_retrieval_page_reflects_state_and_test_result(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        ctx.controller.push_initial_state()
        qapp.processEvents()

        page = window.retrieval_page
        # 先启用引擎（真链路），再直接经桥注入快照（模拟核心回推；Event 不得走请求边界）
        ctx.controller.handle(RetrievalConfigUpdate(engines={"duckduckgo": True}))
        _pump(qapp)

        ctx.bridge.emit_event(
            RetrievalState(
                engines=[
                    {"name": "webfetch", "enabled": False, "needs_key": False,
                     "key_state": "missing", "endpoints": []},
                    {"name": "duckduckgo", "enabled": True, "needs_key": False,
                     "key_state": "missing", "endpoints": ["html.duckduckgo.com"]},
                    {"name": "baidu", "enabled": False, "needs_key": False,
                     "key_state": "missing", "endpoints": ["www.baidu.com"]},
                    {"name": "bing", "enabled": False, "needs_key": False,
                     "key_state": "missing", "endpoints": ["www.bing.com"]},
                    {"name": "arxiv", "enabled": False, "needs_key": False,
                     "key_state": "missing", "endpoints": ["export.arxiv.org"]},
                    {"name": "exa", "enabled": False, "needs_key": True,
                     "key_state": "missing", "endpoints": ["api.exa.ai"]},
                    {"name": "tavily", "enabled": False, "needs_key": True,
                     "key_state": "missing", "endpoints": ["api.tavily.com"]},
                ],
                tools=[
                    {"name": "search.web", "title": "网络搜索", "permission": "confirm", "enabled": True},
                    {"name": "search.fetch", "title": "网页抓取", "permission": "confirm", "enabled": False},
                ],
                default_engines=["duckduckgo"],
                module_state="ready",
            )
        )
        _pump(qapp)
        from gui.widgets.switch import Switch

        switches = page.findChildren(Switch)
        assert len(switches) == 8
        assert page._engine_rows["duckduckgo"]["switch"].isChecked() is True
        assert "html.duckduckgo.com" in page._engine_rows["duckduckgo"]["endpoints"].text()

        # 测试结果回显计数
        page.set_test_result(
            "duckduckgo",
            [{"id": "R2", "status": "pass", "evidence": "", "suggestion": ""}],
            {"pass": 4, "warn": 0, "fail": 0, "skip": 1},
        )
        assert "4✓" in page._engine_rows["duckduckgo"]["badge"].text()
    finally:
        ctx.worker.stop()
        window.close()
