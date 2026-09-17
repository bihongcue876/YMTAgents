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

#: 本地模型服务（Ollama / LM Studio / vLLM）不需要鉴权，但 OpenAI SDK 要求 api_key 非空，
#: 故用一个明确的占位串 —— 它不会被发往任何远程端点（spec rev10 §1）。
LOCAL_API_KEY = "local-no-key"


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
    def reload_settings(self) -> None:
        """重新装载 settings（白名单等）。settings.update 后由 controller 调用。"""

    @abstractmethod
    def test_connection(self, provider_id: str, model_id: str) -> tuple[bool, int | None, str | None]:
        """最小连通性探测，返回 (ok, latency_ms, error_code)。"""

    @abstractmethod
    def list_remote_models(self, provider_id: str) -> tuple[bool, list[str], str | None]:
        """拉取端点自报的模型 ID 列表，返回 (ok, model_ids, error_code)（spec rev9 §2）。"""

    @abstractmethod
    def upsert_provider(self, spec: ProviderSpec, api_key: str | None) -> ProviderSpec:
        """新增或更新供应商；api_key 非空时写入凭据管理器（None = 保持不变）。"""

    @abstractmethod
    def delete_provider(self, provider_id: str) -> bool:
        """删除供应商并清理其凭据；返回是否确有删除。"""

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
    """上游异常 → 带精确 code 的网关异常（spec rev5 §3）。

    归因就原因不就现象：401/403 是凭据问题；404/400/422 在本应用的调用面
    （固定 messages 结构 + max_tokens 探测）里几乎总是「该模型不可用」，
    故归 `model_not_found` 而非笼统的 not_found；其余归 `protocol_error`。
    """
    name = type(exc).__name__
    if isinstance(exc, GatewayError):
        # 已是网关异常：保原码。否则「GatewayBlocked / GatewayAuthError」这类类名
        # 不匹配任何前缀，会被兜底成 network_error，丢掉精确归因（spec rev9 §1）。
        return exc
    if "Authentication" in name or "Permission" in name:
        return GatewayAuthError("凭据不可用")
    if "Timeout" in name:
        # SDK/httpx 的各类超时（APITimeoutError / ReadTimeout / ConnectTimeout）
        # 一律归「静默超时」语义，否则会退化成笼统的 network_error（spec rev8 §2）。
        return GatewayTimeout("静默超时：供应商在时限内未返回数据")
    if "NotFound" in name:
        return GatewayProtocolError(
            "该模型在供应商不可用：供应商未提供此模型", code="model_not_found"
        )
    if "BadRequest" in name or "Unprocessable" in name:
        return GatewayProtocolError(
            "该模型在供应商不可用：供应商拒绝了该模型或请求", code="model_not_found"
        )
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
        # max_retries=0：重试策略由网关自己持有（首 token 前至多 1 次，见 _stream_with_retry）。
        # 若放任 SDK 默认重试，单次 60s 静默会被放大到数倍，A8「60s 无增量即失败」不成立。
        return openai.OpenAI(base_url=base_url, api_key=api_key, max_retries=0)

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
            key_status=self.keyring.status(pc.id) if not pc.local else "missing",
            local=pc.local,
        )

    def _api_key_for(self, pc: ProviderConfig) -> str | None:
        """取该供应商的调用凭据；`None` 表示缺凭据（调用方据此回 `key_missing`）。

        本地服务按 `local` 标记放行：此前一律要求密钥，导致 Ollama / LM Studio
        这类无需鉴权的本地端点永远报 `key_missing`（等于不支持，spec rev10 §1）。
        """
        if pc.local:
            return LOCAL_API_KEY
        return self.keyring.get_key(pc.id)

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
        pc.local = spec.local
        pc.models = [
            ModelConfig(id=m.id, ctx_window=m.ctx_window, tags=list(m.tags))
            for m in spec.models
        ]
        if api_key and not pc.local:
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
        api_key = self._api_key_for(provider)
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

    # -- 拉取端点模型列表（spec rev9 §2） ------------------------------------
    def list_remote_models(self, provider_id: str) -> tuple[bool, list[str], str | None]:
        """GET /models（OpenAI 兼容）取回可用模型 ID，供「模型导入」免手填。

        与 `test_connection` 同一条通道：白名单 → 密钥 → 客户端；错误按 rev5 口径归码。
        取回的是**候选**：不写任何配置，登记与否由用户决定。
        """
        provider = self._find_provider(provider_id)
        if provider is None:
            return False, [], "provider_not_found"
        if not self.whitelist.is_allowed(provider.base_url):
            return False, [], "whitelist_blocked"
        api_key = self._api_key_for(provider)
        if not api_key:
            return False, [], "key_missing"
        client = self._client(provider.base_url, api_key)
        try:
            page = client.models.list(timeout=CONNECT_TIMEOUT_S)
            raw = getattr(page, "data", None) or []
            ids = {str(getattr(m, "id", "")).strip() for m in raw}
        except Exception as exc:  # noqa: BLE001 - 统一归码后返回，不抛给调用方
            return False, [], _map_exception(exc).code
        models = sorted(i for i in ids if i)
        if not models:
            return False, [], "protocol_error"
        return True, models, None

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
            raise GatewayProtocolError(
                f"该模型在供应商不可用：{model_id} 不属于任何已配置的供应商",
                code="model_not_found",
            )
        if not self.whitelist.is_allowed(provider.base_url):
            raise GatewayBlocked("已被网络白名单拦截：该供应商地址不在允许列表内")
        api_key = self._api_key_for(provider)
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
        """单次流式调用。

        静默超时是**双重保险**：
        1. `timeout=` 交给 SDK —— 它约束「两次数据之间的等待」，故真·静默（一个 chunk 都不来）
           也会在阈值处中止；否则循环里的判定永远等不到下一次迭代，取消同样无法生效（spec rev8 §1）。
        2. 循环内判定 —— 兜底自定义客户端（如测试替身）忽略 `timeout` 的情形。
        """
        stream = client.chat.completions.create(
            model=model_id,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            timeout=self.silent_timeout,
        )
        prompt = completion = total = 0
        last = time.monotonic()
        try:
            for chunk in stream:
                now = time.monotonic()
                if cancel_token is not None and cancel_token.is_cancelled():
                    break
                if now - last > self.silent_timeout:
                    raise GatewayTimeout("静默超时")
                # 任何 chunk 都是「链路仍在活动」的证据；只认 content 会把
                # 推理模型的空 content 阶段误判成静默。
                last = now
                choices = getattr(chunk, "choices", None)
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    content = getattr(delta, "content", None) if delta is not None else None
                    if content:
                        on_delta(content)
                        holder["got_delta"] = True
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    prompt = getattr(chunk_usage, "prompt_tokens", 0) or 0
                    completion = getattr(chunk_usage, "completion_tokens", 0) or 0
                    total = getattr(chunk_usage, "total_tokens", 0) or 0
        finally:
            # 中断/异常提前退出时显式关闭 SSE 流，避免连接悬挂到 GC 才释放。
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响既有结果
                    log.debug("关闭模型流失败", exc_info=True)
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total or (prompt + completion),
        )
