"""模型网关（spec §2.2）。

一切模型调用的单通道：BYOK 接入、槽位覆盖、密钥引用、白名单校验、用量计量。
- 客户端按 (base_url, api_key) 缓存；任一变更即重建（切换即生效）。
- 阻塞调用，运行于核心线程；流式增量经 on_delta 回调。
- 静默超时；网络类错误首 token 前重试 ≤1 次；已吐内容不重试。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Callable, Protocol

import openai

from shared.envelope import ModelSpec, ProviderSpec, Usage
from shared.schema import ModelConfig, ModelsConfig, ProviderConfig, SettingsConfig

from core.gateway.errors import (
    GatewayAuthError,
    GatewayBlocked,
    GatewayError,
    GatewayNetworkError,
    GatewayProtocolError,
    GatewayTimeout,
)
from core.gateway.keyring_store import KeyringStore
from core.gateway.meter import Meter
from core.gateway.whitelist import Whitelist, domain_of
from core.store.config_store import ConfigStore

log = logging.getLogger(__name__)

SILENT_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 15.0


class CancelTokenLike(Protocol):
    def is_cancelled(self) -> bool: ...


class IModelGateway(ABC):
    @abstractmethod
    def list_providers(self) -> list[ProviderSpec]: ...

    @abstractmethod
    def get_slots(self) -> dict[str, str | None]: ...

    @abstractmethod
    def set_slot(self, slot: str, model_id: str | None) -> None: ...

    @abstractmethod
    def stream_chat(
        self,
        session_id: str,
        turn_seq: int,
        model_id: str,
        messages: list[dict],
        cancel_token: CancelTokenLike | None,
        on_delta: Callable[[str], None],
    ) -> Usage: ...


def _map_exception(exc: Exception) -> GatewayError:
    name = type(exc).__name__
    if "Authentication" in name or "Permission" in name:
        return GatewayAuthError("凭据不可用")
    if "NotFound" in name or "BadRequest" in name or "Unprocessable" in name:
        return GatewayProtocolError("供应商/模型不存在或请求不合法")
    return GatewayNetworkError("网络或连接错误")


class ModelGateway(IModelGateway):
    def __init__(
        self,
        store: ConfigStore,
        keyring: KeyringStore | None = None,
        client_factory: Callable[[str, str], object] | None = None,
        silent_timeout: float = SILENT_TIMEOUT_S,
    ) -> None:
        self.store = store
        self.keyring = keyring or KeyringStore()
        self.meter = Meter()
        self.silent_timeout = silent_timeout
        self._client_factory = client_factory or self._default_client
        self._clients: dict[tuple[str, str], object] = {}
        self.models: ModelsConfig = store.load("models")
        self.settings: SettingsConfig = store.load("settings")
        self.whitelist = Whitelist(self.settings.network.whitelist)

    # -- 客户端 ------------------------------------------------------------
    @staticmethod
    def _default_client(base_url: str, api_key: str) -> openai.OpenAI:
        return openai.OpenAI(base_url=base_url, api_key=api_key)

    def _client(self, base_url: str, api_key: str) -> object:
        ck = (base_url, api_key)
        if ck not in self._clients:
            self._clients[ck] = self._client_factory(base_url, api_key)
        return self._clients[ck]

    # -- 读取 --------------------------------------------------------------
    def _find_provider(self, provider_id: str) -> ProviderConfig | None:
        for p in self.models.providers:
            if p.id == provider_id:
                return p
        return None

    def _find_provider_for_model(self, model_id: str) -> ProviderConfig | None:
        for p in self.models.providers:
            if any(m.id == model_id for m in p.models):
                return p
        return None

    def _to_spec(self, pc: ProviderConfig) -> ProviderSpec:
        return ProviderSpec(
            id=pc.id,
            name=pc.name,
            base_url=pc.base_url,
            models=[
                ModelSpec(id=m.id, ctx_window=m.ctx_window, tags=list(m.tags))
                for m in pc.models
            ],
            key_status=self.keyring.status(pc.id),
        )

    def list_providers(self) -> list[ProviderSpec]:
        return [self._to_spec(p) for p in self.models.providers]

    def get_slots(self) -> dict[str, str | None]:
        return dict(self.models.slots)

    def set_slot(self, slot: str, model_id: str | None) -> None:
        if slot not in self.models.slots:
            raise GatewayProtocolError(f"未知槽位：{slot}")
        self.models.slots[slot] = model_id  # type: ignore[index]
        self.store.save("models", self.models)

    # -- 配置写入 ----------------------------------------------------------
    def upsert_provider(self, spec: ProviderSpec, api_key: str | None) -> ProviderSpec:
        existing = self._find_provider(spec.id)
        if existing is None:
            pc = ProviderConfig(id=spec.id, name=spec.name, base_url=spec.base_url)
            self.models.providers.append(pc)
        else:
            pc = existing
        pc.name = spec.name
        pc.base_url = spec.base_url
        pc.models = [
            ModelConfig(id=m.id, ctx_window=m.ctx_window, tags=list(m.tags))
            for m in spec.models
        ]
        if api_key:
            self.keyring.set_key(spec.id, api_key)
            pc.key_ref = f"keyring://{self.keyring.service}/{spec.id}"
        self.store.save("models", self.models)

        host = domain_of(spec.base_url)
        if host and self.whitelist.add(host):
            self.settings.network.whitelist = list(self.whitelist.rules)
            self.store.save("settings", self.settings)
        return self._to_spec(pc)

    def delete_provider(self, provider_id: str) -> bool:
        before = len(self.models.providers)
        self.models.providers = [p for p in self.models.providers if p.id != provider_id]
        if len(self.models.providers) == before:
            return False
        for slot, model_id in list(self.models.slots.items()):
            if model_id and self._find_provider_for_model(model_id) is None:
                self.models.slots[slot] = None  # type: ignore[index]
        self.keyring.delete_key(provider_id)
        self.store.save("models", self.models)
        return True

    def reload_settings(self) -> None:
        self.settings = self.store.load("settings")
        self.whitelist = Whitelist(self.settings.network.whitelist)

    # -- 测试连接 ----------------------------------------------------------
    def test_connection(self, provider_id: str, model_id: str) -> tuple[bool, int | None, str | None]:
        """返回 (ok, latency_ms, error_code)。"""
        provider = self._find_provider(provider_id)
        if provider is None:
            return False, None, "provider_not_found"
        if not self.whitelist.is_allowed(provider.base_url):
            return False, None, "whitelist_blocked"
        api_key = self.keyring.get_key(provider_id)
        if not api_key:
            return False, None, "key_missing"
        client = self._client(provider.base_url, api_key)
        start = time.perf_counter()
        try:
            client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                stream=False,
                timeout=CONNECT_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            mapped = _map_exception(exc)
            return False, None, mapped.code
        return True, int((time.perf_counter() - start) * 1000), None

    # -- 流式调用 ----------------------------------------------------------
    def stream_chat(
        self,
        session_id: str,
        turn_seq: int,
        model_id: str,
        messages: list[dict],
        cancel_token: CancelTokenLike | None,
        on_delta: Callable[[str], None],
    ) -> Usage:
        provider = self._find_provider_for_model(model_id)
        if provider is None:
            raise GatewayProtocolError("provider_not_found")
        if not self.whitelist.is_allowed(provider.base_url):
            raise GatewayBlocked("whitelist_blocked")
        api_key = self.keyring.get_key(provider.id)
        if not api_key:
            raise GatewayAuthError("凭据不可用", code="key_missing")
        client = self._client(provider.base_url, api_key)

        usage = self._stream_with_retry(client, model_id, messages, cancel_token, on_delta)
        self.meter.add(usage)
        return usage

    def _stream_with_retry(
        self,
        client: object,
        model_id: str,
        messages: list[dict],
        cancel_token: CancelTokenLike | None,
        on_delta: Callable[[str], None],
    ) -> Usage:
        last_exc: GatewayError | None = None
        for attempt in range(2):  # 初次 + 至多 1 次重试
            holder = {"got_delta": False}
            try:
                return self._stream_once(
                    client, model_id, messages, cancel_token, on_delta, holder
                )
            except GatewayTimeout:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = _map_exception(exc)
                if (
                    holder["got_delta"]
                    or (cancel_token and cancel_token.is_cancelled())
                    or attempt == 1
                ):
                    raise last_exc from exc
                log.info("模型调用首 token 前失败，重试一次：%s", type(exc).__name__)
        assert last_exc is not None
        raise last_exc

    def _stream_once(
        self,
        client: object,
        model_id: str,
        messages: list[dict],
        cancel_token: CancelTokenLike | None,
        on_delta: Callable[[str], None],
        holder: dict,
    ) -> Usage:
        stream = client.chat.completions.create(
            model=model_id,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
        )
        prompt = completion = total = 0
        last = time.monotonic()
        for chunk in stream:
            if cancel_token is not None and cancel_token.is_cancelled():
                break
            if time.monotonic() - last > self.silent_timeout:
                raise GatewayTimeout("静默超时")
            choices = getattr(chunk, "choices", None)
            if choices:
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    on_delta(content)
                    holder["got_delta"] = True
                    last = time.monotonic()
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                prompt = getattr(chunk_usage, "prompt_tokens", 0) or 0
                completion = getattr(chunk_usage, "completion_tokens", 0) or 0
                total = getattr(chunk_usage, "total_tokens", 0) or 0
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total or (prompt + completion),
        )
