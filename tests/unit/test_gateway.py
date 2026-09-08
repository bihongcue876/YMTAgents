"""core.gateway 单元测试（白名单 / 密钥 / 网关流式 / 自动入白名单）。"""

from __future__ import annotations

import pytest

from core.gateway.errors import GatewayAuthError, GatewayBlocked
from core.gateway.keyring_store import KeyringStore
from core.gateway.provider import ModelGateway
from core.gateway.whitelist import Whitelist, domain_of
from core.store.config_store import ConfigStore
from shared.envelope import ModelSpec, ProviderSpec
from shared.schema import ModelConfig, ModelsConfig, ProviderConfig, SettingsConfig


class FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str):
        return self.data.get((service, user))

    def set_password(self, service: str, user: str, password: str) -> None:
        self.data[(service, user)] = password

    def delete_password(self, service: str, user: str) -> None:
        self.data.pop((service, user), None)


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _Delta:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content: str | None = None, usage=None) -> None:
        self.choices = [_Choice(content)] if content is not None else []
        self.usage = usage


class _Completions:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    def create(self, **kwargs):
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
            key_ref="keyring://ymt/prv_1",
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
    backend = FakeKeyring()
    if key:
        backend.set_password("ymt", "prv_1", key)
    gateway = ModelGateway(store, keyring=KeyringStore(backend=backend), client_factory=make_factory(chunks))
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
    gateway = ModelGateway(
        store, keyring=KeyringStore(backend=FakeKeyring()), client_factory=make_factory([])
    )
    spec = ProviderSpec(
        id="prv_x",
        name="X",
        base_url="https://api.newhost.com/v1",
        models=[ModelSpec(id="mx", ctx_window=0)],
    )
    gateway.upsert_provider(spec, "sk-abc")
    assert "api.newhost.com" in gateway.settings.network.whitelist
    assert gateway.list_providers()[0].key_status == "stored"
