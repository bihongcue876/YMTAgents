"""检索模块单元测试（spec-2026-09-25-retrieval §13）。

全部 mock（替换 `engines.open_bounded`），零真实出网；离线解析与策略矩阵。
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

import core.retrieval.engines as eng
from core.retrieval.engines import (
    MIN_INTERVAL_S,
    RetrievalError,
    _extract_text,
    _parse_json_results,
    _search_arxiv,
    _search_baidu,
    _search_bing,
    _search_duckduckgo,
    _unwrap_ddg,
    classify_host,
    search,
)
from core.retrieval.manager import RetrievalManager
from shared.schema import ModulesConfig, NetworkSettings, PluginsConfig, SettingsConfig

ENGINE_ENDPOINTS = eng.ENGINE_ENDPOINTS


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self) -> None:
        self.data = {
            "modules": ModulesConfig(),
            "settings": SettingsConfig(network=NetworkSettings(whitelist=[])),
            "plugins": PluginsConfig(),
        }

    def load(self, name):
        return self.data[name]

    def save(self, name, obj):
        self.data[name] = obj


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
        if getattr(self, "broken", False):
            raise RuntimeError("dpapi unavailable")
        return "stored" if name in self.store else "missing"


class FakeRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, tuple] = {}

    def register(self, spec, handler):
        self.tools[spec.name] = (spec, handler)

    def unregister(self, name):
        self.tools.pop(name, None)


def _manager(**kw):
    store = FakeStore()
    secrets = FakeSecrets()
    registry = FakeRegistry()
    audits: list = []
    mgr = RetrievalManager(
        registry,
        store,
        secrets,
        audit=lambda a, **k: audits.append((a, dict(k))),
        **kw,
    )
    return mgr, store, secrets, registry, audits


# ---------------------------------------------------------------------------
# 引擎解析（fixture 驱动，零出网）
# ---------------------------------------------------------------------------
DDG_HTML = (
    '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fx">标题A</a>'
    '<a class="result__snippet" href="#">摘要A</a>'
    '<a class="result__a" href="https://b.example/y">标题B</a>'
)
BAIDU_HTML = (
    '<h3><a href="http://www.baidu.com/link?url=z">标题一</a></h3>'
    '<div class="c-abstract">摘要一</div>'
    '<h3><a href="https://b.example/y">标题二</a></h3>'
)
BING_HTML = (
    '<li class="b_algo"><h2><a href="https://a.example/x">B1</a></h2>'
    '<p class="b_caption">S1</p></li>'
    '<li class="b_algo"><h2><a href="https://b.example/y">B2</a></h2><p>S2</p></li>'
)
ARXIV_XML = (
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    '<entry><title>T1</title><id>http://arxiv.org/abs/1234.5678</id><summary>Sum1</summary></entry>'
    '<entry><title>T2</title><id>http://arxiv.org/abs/2345.6789</id><summary>Sum2</summary></entry>'
    "</feed>"
)


def test_unwrap_ddg_redirect() -> None:
    assert _unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fa") == "https://a.example/a"
    assert _unwrap_ddg(None) == ""


def test_ddg_parse_unwraps() -> None:
    original = eng.open_bounded
    eng.open_bounded = lambda req, timeout: (DDG_HTML, None)
    try:
        hits = _search_duckduckgo("q", 5, None, 5.0)
    finally:
        eng.open_bounded = original
    assert [h.url for h in hits] == ["https://a.example/x", "https://b.example/y"]
    assert hits[0].snippet == "摘要A"
    assert hits[0].title == "标题A"


def test_baidu_and_bing_parse() -> None:
    original = eng.open_bounded
    eng.open_bounded = lambda req, timeout: (BAIDU_HTML, None)
    try:
        baidu = _search_baidu("q", 5, None, 5.0)
    finally:
        eng.open_bounded = original
    assert [h.title for h in baidu] == ["标题一", "标题二"]
    assert baidu[0].snippet == "摘要一"

    eng.open_bounded = lambda req, timeout: (BING_HTML, None)
    try:
        bing = _search_bing("q", 5, None, 5.0)
    finally:
        eng.open_bounded = original
    assert [(h.title, h.url) for h in bing] == [
        ("B1", "https://a.example/x"),
        ("B2", "https://b.example/y"),
    ]
    assert bing[0].snippet == "S1"


def test_arxiv_parse_https_rewrite() -> None:
    original = eng.open_bounded
    eng.open_bounded = lambda req, timeout: (ARXIV_XML, None)
    try:
        hits = _search_arxiv("quantum", 5, None, 5.0)
    finally:
        eng.open_bounded = original
    assert hits[0].url == "https://arxiv.org/abs/1234.5678"
    assert hits[1].snippet == "Sum2"


def test_json_engine_rows() -> None:
    body = (
        '{"results": [{"title": "E1", "url": "https://e/x", "text": "tx"},'
        ' {"url": "https://e/y", "content": "ct"}]}'
    )
    hits = _parse_json_results(body, 5)
    assert [(h.title, h.url, h.snippet) for h in hits] == [
        ("E1", "https://e/x", "tx"),
        ("", "https://e/y", "ct"),
    ]
    with pytest.raises(RetrievalError):
        _parse_json_results("not-json", 5)


def test_open_error_mapping() -> None:
    def expect(code, exc):
        original = eng.open_bounded
        eng.open_bounded = lambda req, timeout: (_ for _ in ()).throw(exc)
        try:
            with pytest.raises(RetrievalError) as ei:
                eng._open(urllib.request.Request("https://x"), 5.0)
            assert ei.value.code == code
        finally:
            eng.open_bounded = original

    expect("auth_error", urllib.error.HTTPError("https://x", 401, "e", None, None))
    expect("auth_error", urllib.error.HTTPError("https://x", 403, "e", None, None))
    expect("tool_backend_error", urllib.error.HTTPError("https://x", 429, "e", None, None))
    expect("tool_backend_error", urllib.error.HTTPError("https://x", 500, "e", None, None))
    expect("tool_timeout", urllib.error.URLError(TimeoutError()))
    expect("network_error", urllib.error.URLError(OSError("boom")))


def test_classify_host() -> None:
    assert classify_host("localhost") == "loopback"
    assert classify_host("127.0.0.1") == "loopback"
    assert classify_host("192.168.1.10") == "private"
    assert classify_host("172.16.5.5") == "private"
    assert classify_host("no-such-host.invalid") == "public"


def test_extract_text_skips_script_and_truncates() -> None:
    text, title, truncated = _extract_text(
        "<html><head><title>TT</title></head><body><p>Hi</p>"
        "<script>evil()</script><p>there</p></body></html>",
        100,
    )
    assert "Hi" in text and "there" in text and "evil" not in text
    assert title == "TT"
    assert not truncated
    text2, _t, truncated2 = _extract_text("<p>" + "x" * 50 + "</p>", 10)
    assert truncated2 and len(text2) <= 10


def test_search_requires_key_and_known_engine() -> None:
    with pytest.raises(RetrievalError) as ei:
        search("exa", "q", 5, key=None)
    assert ei.value.code == "key_missing"
    with pytest.raises(RetrievalError) as ei2:
        search("tavily", "q", 5, key=None)
    assert ei2.value.code == "key_missing"
    with pytest.raises(RetrievalError) as ei3:
        search("webfetch", "q", 5, key=None)
    assert ei3.value.code == "tool_invalid_args"


# ---------------------------------------------------------------------------
# 宿主（manager）
# ---------------------------------------------------------------------------
def test_activate_registers_and_deactivate_unregisters() -> None:
    mgr, _store, _secrets, registry, _audits = _manager()
    mgr.activate()
    assert set(registry.tools) == {"search.web", "search.fetch"}
    spec, _h = registry.tools["search.web"]
    assert len(spec.description) <= 200
    assert len(spec.prompt_block) <= 600
    assert spec.precheck is not None  # 宿主保留位（对照 mcp./skill. 注册守卫）
    mgr.deactivate()
    assert registry.tools == {}


def test_builtin_toggle_off_unregisters() -> None:
    mgr, _store, _secrets, registry, _audits = _manager()
    mgr.activate()
    mgr.toggle_builtin("search.web", False)
    assert "search.web" not in registry.tools
    assert "search.fetch" in registry.tools
    state = {item["name"]: item for item in mgr.state()}
    assert state["search.web"]["enabled"] is False
    assert state["search.fetch"]["enabled"] is True
    mgr.toggle_builtin("search.web", True)
    assert "search.web" in registry.tools


def test_config_update_whitelist_auto_add_remove() -> None:
    mgr, store, _secrets, _registry, audits = _manager()
    mgr.config_update({"duckduckgo": True}, None)
    assert store.data["settings"].network.whitelist == list(ENGINE_ENDPOINTS["duckduckgo"])
    mgr.config_update({"duckduckgo": False}, None)
    assert store.data["settings"].network.whitelist == []
    actions = [a for a, _k in audits]
    assert "retrieval.whitelist" in actions and "retrieval.engine" in actions
    # 审计不含任何密钥值
    assert all("api_key" not in repr(k) for _a, k in audits)


def test_config_update_unknown_engine_reports() -> None:
    mgr, _store, _secrets, _registry, _audits = _manager()
    emitted: list = []
    mgr._emit = emitted.append  # noqa: SLF001
    mgr.config_update({"nope": True}, None)
    assert any(getattr(e, "code", "") == "invalid_request" for e in emitted)


def test_whitelist_removal_keeps_shared_domains(monkeypatch) -> None:
    endpoints = {"a": ("shared.example", "a.example"), "b": ("shared.example",)}
    monkeypatch.setattr(eng, "ENGINE_ENDPOINTS", endpoints)
    monkeypatch.setattr(eng, "ENGINE_ORDER", ("a", "b"))
    mgr, store, _secrets, _registry, _audits = _manager()
    mgr.config_update({"a": True}, None)
    assert store.data["settings"].network.whitelist == ["shared.example", "a.example"]
    mgr.config_update({"b": True}, None)
    mgr.config_update({"a": False}, None)
    # shared.example 仍被 b 需要 → 保留；a.example 移除
    assert store.data["settings"].network.whitelist == ["shared.example"]
    mgr.config_update({"b": False}, None)
    assert store.data["settings"].network.whitelist == []


def test_web_key_missing_fails_closed() -> None:
    mgr, _store, _secrets, _registry, _audits = _manager()
    mgr.activate()
    mgr.config_update({"exa": True}, None)
    result = mgr.run_web({"query": "q", "engine": "exa"})
    assert result.ok is False and result.error["code"] == "key_missing"


def test_vault_error_maps_to_secret_code() -> None:
    mgr, _store, secrets, _registry, _audits = _manager()
    mgr.activate()
    secrets.broken = True
    assert mgr._key_state("exa") == "error"
    mgr.config_update({"exa": True}, None)
    result = mgr.run_web({"query": "q", "engine": "exa"})
    assert result.ok is False
    assert result.error["code"].startswith("secret")


def test_key_set_uses_vault_namespace_and_audits_without_value() -> None:
    mgr, _store, secrets, _registry, audits = _manager()
    mgr.key_set("exa", "sk-abc")
    assert secrets.store["api_key/retrieval.exa"] == "sk-abc"
    assert [a for a, _k in audits if a.startswith("retrieval.key")] == ["retrieval.key.set"]
    assert "sk-abc" not in repr(audits)
    mgr.key_set("exa", None)
    assert "api_key/retrieval.exa" not in secrets.store
    assert "retrieval.key.clear" in [a for a, _k in audits]


def test_precheck_deny_matrix() -> None:
    mgr, store, _secrets, registry, _audits = _manager()
    mgr.activate()
    pc = registry.tools["search.web"][0].precheck({})
    assert pc and pc[0] == "deny" and "未启用" in pc[1]
    pc2 = registry.tools["search.web"][0].precheck({"engine": "nope"})
    assert pc2 and pc2[0] == "deny" and "未知引擎" in pc2[1]
    pc3 = registry.tools["search.web"][0].precheck({"engine": "webfetch"})
    assert pc3 and pc3[0] == "deny" and "不是搜索引擎" in pc3[1]

    mgr.config_update({"duckduckgo": True}, None)
    assert registry.tools["search.web"][0].precheck({}) is None
    assert registry.tools["search.web"][0].precheck({"engine": "bing"})[0] == "deny"

    pf = registry.tools["search.fetch"][0].precheck
    assert pf({"url": "http://e/x"})[0] == "deny"
    assert pf({"url": "https://localhost/x"})[0] == "deny"
    assert pf({"url": "https://e/x"})[0] == "deny"
    store.data["settings"].network.whitelist = ["e"]
    assert pf({"url": "https://e/x"}) is None


def test_throttle_respects_interval() -> None:
    now = {"t": 0.0}
    sleeps: list = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now["t"] += seconds  # 假时钟随 sleep 前进（否则 while 循环永不退出）

    mgr, _store, _secrets, _registry, _audits = _manager(
        now_fn=lambda: now["t"], sleeper=fake_sleep
    )
    interval = MIN_INTERVAL_S["duckduckgo"]
    mgr._throttle("duckduckgo")
    now["t"] += interval / 2
    mgr._throttle("duckduckgo")
    assert sleeps and sleeps[0] == pytest.approx(interval / 2)
    now["t"] += interval
    sleeps.clear()
    mgr._throttle("duckduckgo")
    assert sleeps == []


def test_fetch_policy_and_run(monkeypatch) -> None:
    mgr, store, _secrets, _registry, audits = _manager()
    mgr.activate()
    assert mgr.run_fetch({"url": "http://e/x"}).error["code"] == "tool_denied"
    assert mgr.run_fetch({"url": "https://localhost/x"}).error["code"] == "tool_denied"
    assert "白名单" in mgr.run_fetch({"url": "https://e/x"}).error["message"]
    store.data["settings"].network.whitelist = ["e"]
    page = "<html><head><title>T</title></head><body><p>Hello <script>bad()</script>world</p></body></html>"
    monkeypatch.setattr(eng, "open_bounded", lambda req, timeout: (page, None))
    result = mgr.run_fetch({"url": "https://e/x"})
    assert result.ok and "Hello" in result.output and "bad()" not in result.output
    assert "retrieval.fetch" in [a for a, _k in audits]


def test_web_run_counts_and_output(monkeypatch) -> None:
    mgr, _store, _secrets, _registry, audits = _manager()
    mgr.activate()
    mgr.config_update({"duckduckgo": True}, None)
    monkeypatch.setattr(eng, "open_bounded", lambda req, timeout: (DDG_HTML, None))
    result = mgr.run_web({"query": "hello"})
    assert result.ok and "example" in result.output
    assert "retrieval.search" in [a for a, _k in audits]
    # count 钳制：99 → 10
    seen: list = []
    original = eng._SEARCHERS["duckduckgo"]

    def spy(query, count, key, timeout):
        seen.append(count)
        return original(query, count, key, timeout)

    monkeypatch.setitem(eng._SEARCHERS, "duckduckgo", spy)
    mgr.run_web({"query": "q", "count": 99})
    assert seen == [10]


def test_engine_test_results() -> None:
    mgr, _store, _secrets, _registry, _audits = _manager()
    emitted: list = []
    mgr._emit = emitted.append  # noqa: SLF001

    # webfetch：全部 skip（无固定端点）
    mgr.test_engine("webfetch")
    result = emitted[-1]
    assert result.engine == "webfetch"
    assert result.summary["skip"] == 5

    # 免 key 引擎：先启用（白名单自动添加 → R2 pass），再 mock 正常返回
    mgr.config_update({"duckduckgo": True}, None)
    original = eng.open_bounded
    eng.open_bounded = lambda req, timeout: (DDG_HTML, None)
    try:
        mgr.test_engine("duckduckgo")
    finally:
        eng.open_bounded = original
    result2 = emitted[-1]
    assert result2.summary["pass"] >= 3
    assert next(f for f in result2.findings if f["id"] == "R2")["status"] == "pass"

    # 网络错误 → R3 fail
    eng.open_bounded = lambda req, timeout: (_ for _ in ()).throw(
        urllib.error.URLError(OSError("down"))
    )
    try:
        mgr.test_engine("duckduckgo")
    finally:
        eng.open_bounded = original
    result3 = emitted[-1]
    assert next(f for f in result3.findings if f["id"] == "R3")["status"] == "fail"

    # 需 key 引擎缺 key → R4 fail（key_missing）
    mgr.test_engine("exa")
    result4 = emitted[-1]
    r4 = next(f for f in result4.findings if f["id"] == "R4")
    assert r4["status"] == "fail"
