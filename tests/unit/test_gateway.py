"""core.gateway 单元测试（白名单 / 密钥 / 网关流式 / 自动入白名单）。"""

from __future__ import annotations

import types

import pytest

from core.gateway.errors import (
    GatewayAuthError,
    GatewayBlocked,
    GatewayProtocolError,
    GatewayTimeout,
)
from core.gateway.provider import ModelGateway
from core.gateway.whitelist import Whitelist, domain_of
from core.store.config_store import ConfigStore
from shared.envelope import ModelSpec, ProviderSpec
from shared.schema import ModelConfig, ModelsConfig, ProviderConfig, SettingsConfig
from tests.mocks.secrets import FakeVault


def make_vault(provider_id: str = "prv_1", key: str = "sk-test") -> FakeVault:
    """构建只含一条 api_key 的内存机密库（v0.0.2 起密钥存本地加密库）。"""
    return FakeVault({f"api_key/{provider_id}": key})


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _Delta:
    def __init__(self, content: str = "", reasoning: str = "") -> None:
        self.content = content
        if reasoning:
            self.reasoning_content = reasoning


class _Choice:
    def __init__(self, content: str = "", reasoning: str = "") -> None:
        self.delta = _Delta(content, reasoning)


class _Chunk:
    def __init__(self, content: str | None = None, usage=None, reasoning: str = "") -> None:
        self.choices = [] if (content is None and not reasoning) else [_Choice(content or "", reasoning)]
        self.usage = usage


class _Completions:
    def __init__(self, chunks) -> None:
        self._chunks = chunks
        self.last_kwargs: dict = {}

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if kwargs.get("stream"):
            return iter(self._chunks)
        return object()


class _Chat:
    def __init__(self, chunks) -> None:
        self.completions = _Completions(chunks)


class _Client:
    def __init__(self, chunks) -> None:
        self.chat = _Chat(chunks)


def make_factory(chunks):
    return lambda base_url, api_key: _Client(chunks)


def seed_store(store: ConfigStore, base_url="https://api.test.com", whitelist=True) -> None:
    models = ModelsConfig()
    models.providers.append(
        ProviderConfig(
            id="prv_1",
            name="P",
            base_url=base_url,
            key_ref="vault://prv_1",
            models=[ModelConfig(id="m1", ctx_window=1000)],
        )
    )
    models.slots["main"] = "m1"
    store.save("models", models)
    settings = SettingsConfig()
    if whitelist:
        settings.network.whitelist = ["api.test.com"]
    store.save("settings", settings)


def make_gateway(tmp_path, chunks, key="sk-test", **kwargs):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    seed_store(store, **kwargs)
    vault = make_vault(key=key) if key else FakeVault()
    gateway = ModelGateway(store, secrets=vault, client_factory=make_factory(chunks))
    return gateway


def test_whitelist_matching():
    wl = Whitelist(["api.example.com", "*.test.com"])
    assert domain_of("https://api.example.com/v1") == "api.example.com"
    assert wl.is_allowed("https://api.example.com")
    assert wl.is_allowed("https://a.test.com")
    assert wl.is_allowed("https://test.com")
    assert not wl.is_allowed("https://evil.com")
    assert not wl.is_allowed("https://example.com")  # 非后缀匹配


def test_stream_chat_usage(tmp_path):
    chunks = [_Chunk("你"), _Chunk("好"), _Chunk(usage=_Usage(10, 2))]
    gateway = make_gateway(tmp_path, chunks)
    out: list[str] = []
    usage = gateway.stream_chat("sess_1", 0, "m1", [{"role": "user", "content": "hi"}], None, out.append)
    assert "".join(out) == "你好"
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 2
    assert gateway.meter.total.total_tokens == 12


class _BadRequestError(Exception):
    """类名含 BadRequest → 网关判定为「端点不接受 tools」的 400。"""


class _ToolsRejectingCompletions:
    """首次带 tools 即抛 400，撤工具后正常返回流（模拟不支持 FC 的端点）。"""

    def __init__(self, chunks) -> None:
        self._chunks = chunks
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tools"):
            raise _BadRequestError("tools unsupported")
        return iter(self._chunks)


class _ToolsRejectingClient:
    def __init__(self, completions) -> None:
        self.chat = types.SimpleNamespace(completions=completions)


def test_stream_chat_falls_back_when_tools_rejected(tmp_path):
    """端点因 tools 返回 400：撤工具重试一次并落盘 tools_unsupported（rev42 完善）。"""
    chunks = [_Chunk("好"), _Chunk(usage=_Usage(5, 1))]
    completions = _ToolsRejectingCompletions(chunks)
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    seed_store(store)
    gateway = ModelGateway(
        store,
        secrets=make_vault(),
        client_factory=lambda base_url, api_key: _ToolsRejectingClient(completions),
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "mcp.a.t",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    out: list[str] = []
    usage = gateway.stream_chat(
        "sess_1", 0, "m1", [{"role": "user", "content": "hi"}], None, out.append, tools=tools
    )
    assert "".join(out) == "好"
    assert usage.total_tokens == 6
    assert completions.calls[0].get("tools")  # 第一次带工具
    assert completions.calls[1].get("tools") is None  # 撤工具重试
    models = ConfigStore(tmp_path).load("models")
    model = next(m for p in models.providers for m in p.models if m.id == "m1")
    assert model.tools_unsupported is True

    # 已标记：后续调用直接不带工具（不再触发一次 400）
    completions.calls.clear()
    gateway.stream_chat(
        "sess_1", 0, "m1", [{"role": "user", "content": "hi"}], None, out.append, tools=tools
    )
    assert completions.calls and completions.calls[0].get("tools") is None


def test_stream_chat_blocked(tmp_path):
    gateway = make_gateway(tmp_path, [], whitelist=False)
    with pytest.raises(GatewayBlocked):
        gateway.stream_chat("sess_1", 0, "m1", [], None, lambda _s: None)


def test_stream_chat_missing_key(tmp_path):
    gateway = make_gateway(tmp_path, [], key=None)
    with pytest.raises(GatewayAuthError):
        gateway.stream_chat("sess_1", 0, "m1", [], None, lambda _s: None)


def test_upsert_provider_adds_whitelist(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    gateway = ModelGateway(store, secrets=FakeVault(), client_factory=make_factory([]))
    spec = ProviderSpec(
        id="prv_x",
        name="X",
        base_url="https://api.newhost.com/v1",
        models=[ModelSpec(id="mx", ctx_window=0)],
    )
    gateway.upsert_provider(spec, "sk-abc")
    assert "api.newhost.com" in gateway.settings.network.whitelist
    assert gateway.list_providers()[0].key_status == "stored"


# -- 传输保密性（rev15） ------------------------------------------------------


def test_upsert_rejects_insecure_transport(tmp_path):
    """非本机 http:// 在**入口**就被拒：凭据与对话内容不得明文过网。"""
    gateway = _bare_gateway(tmp_path)
    spec = ProviderSpec(
        id="prv_bad",
        name="Bad",
        base_url="http://api.evil.com/v1",
        models=[ModelSpec(id="mx", ctx_window=0)],
    )
    with pytest.raises(GatewayBlocked) as ei:
        gateway.upsert_provider(spec, "sk-abc")
    assert ei.value.code == "insecure_transport"
    assert gateway._find_provider("prv_bad") is None  # 未落盘


def test_upsert_allows_loopback_http(tmp_path):
    """本地模型服务的 http://127.0.0.1 是唯一明文例外（流量不出机器）。"""
    gateway = _bare_gateway(tmp_path)
    spec = ProviderSpec(
        id="prv_local",
        name="Local",
        base_url="http://127.0.0.1:11434/v1",
        models=[ModelSpec(id="m-local", ctx_window=0)],
        local=True,
    )
    gateway.upsert_provider(spec, None)
    assert gateway._find_provider("prv_local") is not None


def _seeded_insecure_gateway(tmp_path, base_url: str) -> ModelGateway:
    """手工改配置文件绕过入口校验的情形：调用点仍要拦（双保险）。"""
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    models = ModelsConfig()
    models.providers.append(
        ProviderConfig(
            id="prv_1",
            name="P",
            base_url=base_url,
            key_ref="vault://prv_1",
            models=[ModelConfig(id="m1", ctx_window=1000)],
        )
    )
    models.slots["main"] = "m1"
    store.save("models", models)
    settings = SettingsConfig()
    settings.network.whitelist = ["api.test.com"]
    store.save("settings", settings)
    backend = make_vault()
    return ModelGateway(
        store, secrets=backend, client_factory=make_factory([])
    )


def test_stream_chat_blocks_insecure_transport(tmp_path):
    gateway = _seeded_insecure_gateway(tmp_path, "http://api.test.com/v1")
    with pytest.raises(GatewayBlocked) as ei:
        gateway.stream_chat("sess_1", 0, "m1", [], None, lambda _s: None)
    assert ei.value.code == "insecure_transport"


def test_test_connection_reports_insecure_transport(tmp_path):
    gateway = _seeded_insecure_gateway(tmp_path, "http://api.test.com/v1")
    ok, latency, code = gateway.test_connection("prv_1", "m1")
    assert ok is False
    assert code == "insecure_transport"


def test_list_remote_models_reports_insecure_transport(tmp_path):
    gateway = _seeded_insecure_gateway(tmp_path, "http://api.test.com/v1")
    ok, models, code = gateway.list_remote_models("prv_1")
    assert ok is False
    assert code == "insecure_transport"


# -- 全局槽位绑定（spec rev4 §3） --------------------------------------------


def _bare_gateway(tmp_path) -> ModelGateway:
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    return ModelGateway(store, secrets=FakeVault(), client_factory=make_factory([]))


def test_set_slot_persists_to_models_json(tmp_path):
    """绑定写 models.json（含 .bak），且以文件为准可被重新装载（03 §10）。"""
    gateway = _bare_gateway(tmp_path)
    assert gateway.get_slots()["main"] is None

    gateway.set_slot("main", "m1")
    assert gateway.get_slots()["main"] == "m1"
    assert ConfigStore(tmp_path).load("models").slots["main"] == "m1"
    assert (tmp_path / "config" / "models.json.bak").exists()

    gateway.set_slot("main", None)
    assert ConfigStore(tmp_path).load("models").slots["main"] is None


def test_set_slot_rejects_unknown_slot(tmp_path):
    gateway = _bare_gateway(tmp_path)
    with pytest.raises(GatewayProtocolError):
        gateway.set_slot("nope", "m1")


# -- 错误归因与中文提示（spec rev5 §2/§3） ----------------------------------


def test_map_exception_attributes_precise_codes():
    """上游异常按真实原因归码，不再一律 provider_not_found。

    回归锚点：此前 Authentication/NotFound/BadRequest 三种原因压成同一个 not_found 码。
    `_map_exception` 按异常类名归因，故此处用同名假异常。
    """

    class AuthenticationError(Exception):
        pass

    class NotFoundError(Exception):
        pass

    class BadRequestError(Exception):
        pass

    from core.gateway.provider import _map_exception

    assert _map_exception(AuthenticationError()).code == "auth_error"
    assert _map_exception(NotFoundError()).code == "model_not_found"
    assert _map_exception(BadRequestError()).code == "model_not_found"
    assert _map_exception(RuntimeError()).code == "network_error"


def test_stream_chat_unknown_model_reports_model_not_found(tmp_path):
    """模型不属于任何已配置供应商 → model_not_found（而非 provider_not_found）。"""
    gateway = _bare_gateway(tmp_path)
    with pytest.raises(GatewayProtocolError) as ei:
        gateway.stream_chat("sess_1", 0, "not-a-model", [], None, lambda _s: None)
    assert ei.value.code == "model_not_found"


def test_gateway_protocol_error_no_longer_defaults_to_provider_not_found():
    """GatewayProtocolError 默认码为 protocol_error，不再冒充 not_found。"""
    assert GatewayProtocolError("x").code == "protocol_error"


def test_error_text_covers_every_code():
    """每个错误码都必须有中文提示（前端展示来源，rev5 §2）。"""
    from shared.errors import ERROR_TEXT, ErrorCode

    assert set(ERROR_TEXT) == {c.value for c in ErrorCode}
    assert all(isinstance(v, str) and v.strip() for v in ERROR_TEXT.values())


# -- 静默超时与取消（spec rev8 §1） ------------------------------------------


class _StallingStream:
    """静默 silent_s 秒后才吐第一块；用于验证「真·静默」不再无法中止。"""

    def __init__(self, silent_s: float, chunks=()) -> None:
        self._silent_s = silent_s
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        import time

        time.sleep(self._silent_s)
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _CapturingClient:
    """记录 create() 实收参数，并返回指定流。"""

    def __init__(self, stream_factory) -> None:
        self.kwargs: dict = {}
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.kwargs.update(kw)
                return stream_factory()

        self.chat = types.SimpleNamespace(completions=_Completions())


def make_gateway_with_client(tmp_path, client, silent_timeout: float | None = None) -> ModelGateway:
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    seed_store(store)
    backend = make_vault()
    kw = {} if silent_timeout is None else {"silent_timeout": silent_timeout}
    return ModelGateway(
        store, secrets=backend, client_factory=lambda _b, _k: client, **kw
    )


def test_stream_chat_passes_silence_timeout_to_client(tmp_path):
    """回归锚点：create() 必须带 timeout，否则真·静默无法中止。

    实证：旧实现既不传 timeout，又只在下一次 chunk 到达时才判定静默 ——
    静默 2.0s 而阈值 0.5s 时耗时 2.00s 且**正常返回**（SDK 默认 600s 且自带重试）。
    """
    client = _CapturingClient(lambda: iter([]))
    gateway = make_gateway_with_client(tmp_path, client, silent_timeout=0.5)
    gateway.stream_chat("sess_1", 0, "m1", [{"role": "user", "content": "hi"}], None, lambda _s: None)
    assert client.kwargs.get("timeout") == 0.5
    assert client.kwargs.get("stream") is True


def test_stalled_stream_raises_gateway_timeout_and_closes_stream(tmp_path):
    """替身不理会 timeout 时由循环内判定兜底；提前退出必须关闭流。"""
    stream = _StallingStream(0.3, [_Chunk("迟到")])
    gateway = make_gateway_with_client(
        tmp_path, _CapturingClient(lambda: stream), silent_timeout=0.05
    )
    with pytest.raises(GatewayTimeout):
        gateway.stream_chat(
            "sess_1", 0, "m1", [{"role": "user", "content": "hi"}], None, lambda _s: None
        )
    assert stream.closed is True


def test_timeout_exceptions_map_to_gateway_timeout():
    """SDK/httpx 超时归「静默超时」，不得退化成笼统 network_error（A8 语义）。"""
    from core.gateway.provider import _map_exception

    class APITimeoutError(Exception):
        pass

    class ReadTimeout(Exception):
        pass

    for exc in (APITimeoutError(), ReadTimeout()):
        mapped = _map_exception(exc)
        assert isinstance(mapped, GatewayTimeout)
        assert mapped.code == "network_error"
        assert "静默超时" in str(mapped)


def test_default_client_disables_sdk_level_retries():
    """重试策略归网关所有：SDK 默认重试会把单次 60s 静默放大数倍。"""
    client = ModelGateway._default_client("https://api.test.com/v1", "sk-x")
    assert client.max_retries == 0


def test_map_exception_preserves_gateway_error_code():
    """回归锚点：网关异常再次归因会退化成 network_error，丢掉精确码（spec rev9 §1）。"""
    from core.gateway.provider import _map_exception

    original = GatewayBlocked("已被网络白名单拦截")
    mapped = _map_exception(original)
    assert mapped is original
    assert mapped.code == "whitelist_blocked"


# -- 端点模型列表：模型导入免手填（spec rev9 §2） -----------------------------


class _ModelObj:
    def __init__(self, model_id: str) -> None:
        self.id = model_id


class _ModelsResult:
    def __init__(self, ids: list[str]) -> None:
        self.data = [_ModelObj(i) for i in ids]


class _ModelsEndpoint:
    def __init__(self, ids: list[str], exc: Exception | None = None) -> None:
        self._ids = ids
        self._exc = exc

    def list(self, **_kwargs):
        if self._exc is not None:
            raise self._exc
        return _ModelsResult(self._ids)


class _ModelListClient:
    """只实现 models.list 的替身（list_remote_models 不触碰 chat）。"""

    def __init__(self, ids: list[str], exc: Exception | None = None) -> None:
        self.models = _ModelsEndpoint(ids, exc)
        self.chat = types.SimpleNamespace(completions=None)


def test_list_remote_models_returns_sorted_unique_ids(tmp_path):
    client = _ModelListClient(["m2", "m1 ", "m1", ""])
    gateway = make_gateway_with_client(tmp_path, client)
    ok, models, error = gateway.list_remote_models("prv_1")
    assert ok is True and error is None
    assert models == ["m1", "m2"]  # 去空白、去重、排序


def test_list_remote_models_reports_reason_codes(tmp_path):
    gateway = make_gateway_with_client(tmp_path, _ModelListClient(["m1"]))
    assert gateway.list_remote_models("不存在") == (False, [], "provider_not_found")

    blocked = make_gateway(tmp_path, [], whitelist=False)
    assert blocked.list_remote_models("prv_1")[2] == "whitelist_blocked"

    no_key = make_gateway(tmp_path, [], key=None)
    assert no_key.list_remote_models("prv_1")[2] == "key_missing"


def test_list_remote_models_maps_upstream_errors(tmp_path):
    class AuthenticationError(Exception):
        pass

    auth = make_gateway_with_client(tmp_path, _ModelListClient([], AuthenticationError()))
    assert auth.list_remote_models("prv_1") == (False, [], "auth_error")

    network = make_gateway_with_client(tmp_path, _ModelListClient([], RuntimeError()))
    assert network.list_remote_models("prv_1") == (False, [], "network_error")

    empty = make_gateway_with_client(tmp_path, _ModelListClient([]))
    assert empty.list_remote_models("prv_1") == (False, [], "protocol_error")


# -- 本地模型服务：免密钥（spec rev10 §1） ------------------------------------


def test_is_local_url_recognises_loopback():
    from shared.net import is_local_url

    for url in (
        "http://127.0.0.1:11434/v1",
        "http://localhost:1234/v1",
        "127.0.0.1:8000",
        "http://0.0.0.0:9997/v1",
        "http://[::1]:8080/v1",
    ):
        assert is_local_url(url), url
    for url in ("https://api.deepseek.com/v1", "https://api.openai.com/v1", ""):
        assert not is_local_url(url), url


class _LocalCompletions:
    def create(self, **_kwargs):
        return object()  # 非流式探测：只要求不抛


class _LocalClient:
    """同时具备 models.list 与 chat.completions.create 的本地服务替身。"""

    def __init__(self, ids: list[str]) -> None:
        self.models = _ModelsEndpoint(ids)
        self.chat = types.SimpleNamespace(completions=_LocalCompletions())


def seed_local_store(store: ConfigStore, base_url="http://127.0.0.1:11434/v1") -> None:
    models = ModelsConfig()
    models.providers.append(
        ProviderConfig(
            id="prv_local",
            name="Ollama",
            base_url=base_url,
            local=True,
            models=[ModelConfig(id="llama3", ctx_window=8192)],
        )
    )
    models.slots["main"] = "llama3"
    store.save("models", models)
    settings = SettingsConfig()
    settings.network.whitelist = ["127.0.0.1"]
    store.save("settings", settings)


def make_local_gateway(tmp_path, client, backend=None) -> ModelGateway:
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    seed_local_store(store)
    return ModelGateway(
        store,
        secrets=backend or FakeVault(),
        client_factory=lambda _b, _k: client,
    )


def test_local_provider_works_without_api_key(tmp_path):
    """回归锚点：本地服务此前一律要求密钥 —— Ollama / LM Studio 永远 `key_missing`（等于不支持）。

    现在 `local=True` 的供应商三条路径都放行：取模型列表 / 测试连接 / 流式对话。
    """
    gateway = make_local_gateway(tmp_path, _LocalClient(["llama3"]))
    assert gateway.list_remote_models("prv_local") == (True, ["llama3"], None)
    assert gateway.test_connection("prv_local", "llama3")[0] is True

    streamed: list[str] = []
    store_gateway = make_local_gateway(tmp_path, _Client([_Chunk("本地"), _Chunk(usage=_Usage(3, 1))]))
    usage = store_gateway.stream_chat(
        "sess", 0, "llama3", [{"role": "user", "content": "hi"}], None, streamed.append
    )
    assert "".join(streamed) == "本地"
    assert usage.total_tokens == 4
    assert gateway.list_providers()[0].local is True


def test_upsert_local_provider_does_not_store_key(tmp_path):
    """本地类型即便被塞了密钥也不写机密库；`local` 标记随配置落盘。"""
    backend = FakeVault()
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    gateway = ModelGateway(
        store, secrets=backend, client_factory=make_factory([])
    )
    gateway.upsert_provider(
        ProviderSpec(
            id="prv_l",
            name="Ollama",
            base_url="http://127.0.0.1:11434/v1",
            models=[ModelSpec(id="llama3")],
            local=True,
        ),
        "不该被存储",
    )
    assert backend.data == {}
    assert gateway.list_providers()[0].local is True
    assert ConfigStore(tmp_path).load("models").providers[0].local is True
    assert "127.0.0.1" in gateway.settings.network.whitelist  # 本地地址同样进白名单


def test_remote_provider_still_requires_key(tmp_path):
    """本地放行不得放宽远端：缺密钥仍是 `key_missing`。"""
    gateway = make_gateway(tmp_path, [], key=None)
    assert gateway.list_remote_models("prv_1")[2] == "key_missing"
    assert gateway.test_connection("prv_1", "m1")[2] == "key_missing"


# -- 思考能力探测与采集（spec rev25） ----------------------------------------


class BadRequestError(Exception):
    """类名含 BadRequest → `_probe_once` 归为「参数被拒」（400/422 同义）。"""


class _RejectParamClient:
    """带 `reasoning_effort` 即报错；去掉参数后正常回流思考。"""

    def __init__(self, chunks) -> None:
        self.kwargs: dict = {}
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.kwargs.update(kw)
                if kw.get("reasoning_effort"):
                    raise BadRequestError("reasoning_effort unsupported")
                return iter(chunks)

        self.chat = types.SimpleNamespace(completions=_Completions())


def _gateway_on_existing_store(tmp_path, client) -> ModelGateway:
    return ModelGateway(
        ConfigStore(tmp_path), secrets=make_vault(), client_factory=lambda _b, _k: client
    )


def test_stream_chat_captures_reasoning_and_generation_timing(tmp_path):
    """思考与正文分流；Usage 记录首 token / 末 token 时刻（生成阶段 TPS 来源）。"""
    chunks = [
        _Chunk(reasoning="先想"),
        _Chunk(reasoning="再想"),
        _Chunk("答案"),
        _Chunk(usage=_Usage(10, 3)),
    ]
    gateway = make_gateway(tmp_path, chunks)
    content: list[str] = []
    thoughts: list[str] = []
    usage = gateway.stream_chat(
        "s", 0, "m1", [{"role": "user", "content": "hi"}], None, content.append,
        on_reasoning=thoughts.append,
    )
    assert "".join(thoughts) == "先想再想"
    assert "".join(content) == "答案"
    assert usage.total_tokens == 13
    assert usage.elapsed_ms >= usage.first_token_ms >= 0


def test_probe_reasoning_records_yes_and_enables_param(tmp_path):
    gateway = make_gateway(tmp_path, [_Chunk(reasoning="hmm"), _Chunk(usage=_Usage(5, 1))])
    assert gateway.reasoning_pending("m1") is True

    assert gateway.probe_reasoning("m1") == "yes"
    assert gateway.reasoning_pending("m1") is False  # 不重复探测
    saved = ConfigStore(tmp_path).load("models").providers[0].models[0]
    assert saved.reasoning_detected == "yes"
    assert saved.reasoning_param_ok is True

    client = _CapturingClient(lambda: iter([_Chunk("答"), _Chunk(usage=_Usage(5, 1))]))
    gw2 = _gateway_on_existing_store(tmp_path, client)
    gw2.stream_chat("s", 0, "m1", [{"role": "user", "content": "hi"}], None, lambda _s: None)
    assert client.kwargs.get("reasoning_effort") == "low"


def test_probe_reasoning_records_no(tmp_path):
    gateway = make_gateway(tmp_path, [_Chunk("ok"), _Chunk(usage=_Usage(5, 1))])
    assert gateway.probe_reasoning("m1") == "no"
    saved = ConfigStore(tmp_path).load("models").providers[0].models[0]
    assert saved.reasoning_detected == "no"
    assert saved.reasoning_param_ok is True


def test_probe_reasoning_falls_back_when_param_rejected(tmp_path):
    """端点拒绝 `reasoning_effort` 时改被动采集：detected=yes 但不再下发参数。"""
    client = _RejectParamClient([_Chunk(reasoning="hmm"), _Chunk(usage=_Usage(5, 1))])
    gateway = make_gateway_with_client(tmp_path, client)
    assert gateway.probe_reasoning("m1") == "yes"
    saved = ConfigStore(tmp_path).load("models").providers[0].models[0]
    assert saved.reasoning_detected == "yes"
    assert saved.reasoning_param_ok is False

    client.kwargs.clear()
    gateway.stream_chat("s", 0, "m1", [{"role": "user", "content": "hi"}], None, lambda _s: None)
    assert "reasoning_effort" not in client.kwargs


class _BoomClient:
    """探测时直接抛非 400/422 异常 → 归 unknown。"""

    def __init__(self) -> None:
        outer = self

        class _Completions:
            def create(self, **kw):
                raise RuntimeError("connection reset")

        self.chat = types.SimpleNamespace(completions=_Completions())


def test_probe_reasoning_unknown_does_not_block(tmp_path):
    """节点不可达/失败一律 unknown，且不写缓存（端点事实不臆断）。"""
    gateway = make_gateway_with_client(tmp_path, _BoomClient())
    assert gateway.probe_reasoning("m1") == "unknown"
    assert ConfigStore(tmp_path).load("models").providers[0].models[0].reasoning_detected == "unknown"


def test_probe_unknown_not_retried_in_process(tmp_path):
    """rev27：探测未得结论，本进程也不再每回合重探（用户裁决「不每次都测」）。"""
    gateway = make_gateway_with_client(tmp_path, _BoomClient())
    assert gateway.reasoning_pending("m1") is True
    assert gateway.probe_reasoning("m1") == "unknown"
    assert gateway.reasoning_pending("m1") is False  # 同进程不再重试

    # 新进程（新网关实例）仍会再试一次 —— 未得结论不落盘。
    fresh = make_gateway_with_client(tmp_path, _BoomClient())
    assert fresh.reasoning_pending("m1") is True


def test_upsert_provider_preserves_reasoning_detection(tmp_path):
    """编辑模型表不得清空探测缓存（端点事实），偏好以本次提交为准。"""
    gateway = make_gateway(tmp_path, [_Chunk(reasoning="hmm"), _Chunk(usage=_Usage(5, 1))])
    gateway.probe_reasoning("m1")

    gateway.upsert_provider(
        ProviderSpec(
            id="prv_1",
            name="P",
            base_url="https://api.test.com",
            models=[ModelSpec(id="m1", ctx_window=2000, reasoning="off")],
        ),
        "sk-test",
    )
    reloaded = ConfigStore(tmp_path).load("models").providers[0].models[0]
    assert reloaded.reasoning_detected == "yes"
    assert reloaded.reasoning_param_ok is True
    assert reloaded.reasoning == "off"

