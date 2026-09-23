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
from typing import Any, Callable, Protocol

from shared.envelope import ModelSpec, ProviderSpec, SessionParams, Usage
from shared.net import is_secure_transport
from shared.schema import ModelConfig, ModelsConfig, ProviderConfig, SettingsConfig

from core.gateway.errors import (
    GatewayAuthError,
    GatewayBlocked,
    GatewayError,
    GatewayNetworkError,
    GatewayProtocolError,
    GatewayTimeout,
)
from core.gateway.meter import Meter
from core.gateway.whitelist import Whitelist, domain_of
from core.security.dpapi import DpapiBox
from core.security.vault import ISecretStore, Vault, key_ref_for, secret_name
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
        """新增或更新供应商；api_key 非空时写入本地加密机密库（None = 保持不变）。"""

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
        on_reasoning: Callable[[str], None] | None = None,
        params: SessionParams | None = None,
        tools: list[dict] | None = None,
        on_tool_calls: Callable[[list[dict]], None] | None = None,
    ) -> Usage:
        """流式对话。`params` 为会话级模型参数（rev24）：仅下发用户显式启用的项。

        `on_reasoning`（rev25）：思考过程增量回调（正文与思考分流）；模型不支持时为 None。
        `tools`（v0.0.3）：native function calling 工具定义；`on_tool_calls` 在流结束后
        一次性回传聚合的 tool_calls（无则回调）。端点若因 tools 返回 400，网关自动撤工具
        重试一次并标记该模型（`tools_unsupported`），不再下发工具。
        """

    @abstractmethod
    def reasoning_pending(self, model_id: str) -> bool:
        """该模型的思考能力是否尚未确定（需要调用前探测，rev25）。"""

    @abstractmethod
    def probe_reasoning(self, model_id: str) -> str:
        """一次性探测思考能力，返回 "yes"/"no"/"unknown"，结果写入 models.json 缓存。"""

    @abstractmethod
    def generate_title(self, messages: list[dict], model_id: str) -> str:
        """模型提炼单行会话标题（→调用方截断，本方法只须返回单次非流式短输出）。

        供新会话首轮回复后自动生成标题（rev59）。复用底层一次对话通道（非流式，短输出），
        不发 `max_tokens`（承 rev20）。返回空串或调用异常 → 由调用方走截断回退。
        """


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


def _param_options(params: SessionParams | None) -> dict:
    """会话级参数 → OpenAI 兼容关键字；`None` 一律不下发（沿用供应商默认，rev24）。"""
    if params is None:
        return {}
    options: dict = {}
    for name in ("temperature", "top_p", "top_k", "max_tokens"):
        value = getattr(params, name, None)
        if value is not None:
            options[name] = value
    return options


def _merge_tool_calls(slot: list[dict], deltas) -> None:
    """把流式 tool_calls 增量按 index 归并到 slot（name 整段到达，arguments 分片追加）。"""
    for tc in deltas:
        idx = getattr(tc, "index", 0) or 0
        while len(slot) <= idx:
            slot.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        entry = slot[idx]
        tc_id = getattr(tc, "id", None)
        if tc_id:
            entry["id"] = tc_id
        fn = getattr(tc, "function", None)
        if fn is None:
            continue
        name = getattr(fn, "name", None)
        if name:
            entry["function"]["name"] = name
        args = getattr(fn, "arguments", None)
        if args:
            entry["function"]["arguments"] += args


def _reasoning_text(delta) -> str:
    """从流式 delta 中取思考文本（rev25）。

    各家的字段名不同：DeepSeek / vLLM / 硅基流动用 `reasoning_content`，
    OpenRouter 等用 `reasoning`。取到非空即视为思考增量。
    """
    if delta is None:
        return ""
    for name in ("reasoning_content", "reasoning"):
        value = getattr(delta, name, None)
        if isinstance(value, str) and value:
            return value
    return ""


class ModelGateway(IModelGateway):
    def __init__(
        self,
        store: ConfigStore,
        secrets: ISecretStore | None = None,
        client_factory: Callable[[str, str], object] | None = None,
        silent_timeout: float = SILENT_TIMEOUT_S,
    ) -> None:
        self.store = store
        self.secrets = secrets or Vault(store.root, DpapiBox())
        self.meter = Meter()
        self.silent_timeout = silent_timeout
        self._client_factory = client_factory or self._default_client
        self._clients: dict[tuple[str, str], object] = {}
        self.models: ModelsConfig = store.load("models")
        self.settings: SettingsConfig = store.load("settings")
        self.whitelist = Whitelist(self.settings.network.whitelist)
        # rev27：本进程内已探测过的模型 —— 探测即使未得结论也不再每回合重试（用户裁决「不每次都测」）。
        self._probe_attempted: set[str] = set()

    # -- 客户端 ------------------------------------------------------------
    @staticmethod
    def _default_client(base_url: str, api_key: str) -> Any:
        # openai SDK 惰性导入：它拖带 pydantic 全家 + httpx，import 占 ~1s，
        # 而 GUI 启动根本不碰网关 —— 推迟到真正建客户端时（启动提速 rev55）。
        # max_retries=0：重试策略由网关自己持有（首 token 前至多 1 次，见 _stream_with_retry）。
        # 若放任 SDK 默认重试，单次 60s 静默会被放大到数倍，A8「60s 无增量即失败」不成立。
        import openai

        return openai.OpenAI(base_url=base_url, api_key=api_key, max_retries=0)

    def _client(self, base_url: str, api_key: str) -> object:
        ck = (base_url, api_key)
        if ck not in self._clients:
            self._clients[ck] = self._client_factory(base_url, api_key)
        return self._clients[ck]

    # -- 传输安全（rev15） ---------------------------------------------------
    @staticmethod
    def _ensure_secure_transport(base_url: str) -> None:
        """凭据与对话内容不得走明文：非本机地址必须 https（本机回环的本地服务除外）。

        入口（upsert）校验 + 调用点（test/models/chat）防御双保险：
        手工改配置文件绕过入口时，调用仍会被拦。
        """
        if not is_secure_transport(base_url):
            raise GatewayBlocked(
                "明文传输不安全：非本机地址必须使用 https://",
                code="insecure_transport",
            )

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
                ModelSpec(
                    id=m.id,
                    ctx_window=m.ctx_window,
                    tags=list(m.tags),
                    reasoning=m.reasoning,
                    reasoning_detected=m.reasoning_detected,
                )
                for m in pc.models
            ],
            key_status=self.secrets.status(secret_name(pc.id)) if not pc.local else "missing",
            local=pc.local,
        )

    def _api_key_for(self, pc: ProviderConfig) -> str | None:
        """取该供应商的调用凭据；`None` 表示缺凭据（调用方据此回 `key_missing`）。

        本地服务按 `local` 标记放行：此前一律要求密钥，导致 Ollama / LM Studio
        这类无需鉴权的本地端点永远报 `key_missing`（等于不支持，spec rev10 §1）。
        """
        if pc.local:
            return LOCAL_API_KEY
        return self.secrets.get(secret_name(pc.id))

    def list_providers(self) -> list[ProviderSpec]:
        return [self._to_spec(p) for p in self.models.providers]

    # -- 思考能力（rev25） ---------------------------------------------------
    def _find_model(self, model_id: str) -> ModelConfig | None:
        for p in self.models.providers:
            for m in p.models:
                if m.id == model_id:
                    return m
        return None

    def reasoning_pending(self, model_id: str) -> bool:
        """偏好为 auto、尚未得结论、且本进程未探测过 → 需要一次调用前探测。

        rev27：探测失败（unknown）也记入 `_probe_attempted`，避免每回合重复探测与重复预告。
        """
        m = self._find_model(model_id)
        return bool(
            m is not None
            and m.reasoning == "auto"
            and m.reasoning_detected == "unknown"
            and model_id not in self._probe_attempted
        )

    def _reasoning_enabled(self, model_id: str) -> bool:
        """是否显示/采集思考：人工覆盖优先，auto 用探测结果（yes 才启用）。"""
        m = self._find_model(model_id)
        if m is None:
            return False
        if m.reasoning == "on":
            return True
        if m.reasoning == "off":
            return False
        return m.reasoning_detected == "yes"

    def _reasoning_options(self, model_id: str) -> dict:
        """主动下发思考参数（仅当探测确认该端点接受 `reasoning_effort`，rev25）。"""
        m = self._find_model(model_id)
        if m is None or not m.reasoning_param_ok or not self._reasoning_enabled(model_id):
            return {}
        return {"reasoning_effort": "low"}

    def _record_reasoning(self, model_id: str, detected: str, param_ok: bool) -> None:
        m = self._find_model(model_id)
        if m is None:
            return
        m.reasoning_detected = detected  # type: ignore[assignment]
        m.reasoning_param_ok = param_ok
        self.store.save("models", self.models)

    def probe_reasoning(self, model_id: str) -> str:
        """一次极小探测（成本 ≈ W 提示 + 64 输出 token）：先带 reasoning_effort，被拒则去掉重探。

        归 "yes"/"no"/"unknown"；任何网络/配置异常一律 unknown（不误判、不影响对话）。
        得结论则写入 models.json（rev25：不每次都测）；未得结论也记入内存 `_probe_attempted`，
        本进程不再重复探测（rev27）。
        """
        m = self._find_model(model_id)
        provider = self._find_provider_for_model(model_id)
        if m is None or provider is None:
            return "unknown"
        self._probe_attempted.add(model_id)  # rev27：无论成败，本进程不再重复探测
        try:
            self._ensure_secure_transport(provider.base_url)
            if not self.whitelist.is_allowed(provider.base_url):
                return "unknown"
            api_key = self._api_key_for(provider)
            if not api_key:
                return "unknown"
        except GatewayError:
            return "unknown"
        client = self._client(provider.base_url, api_key)
        with_param = self._probe_once(client, model_id, reasoning_effort="low")
        if with_param == "rejected":
            without = self._probe_once(client, model_id, reasoning_effort=None)
            if without in ("yes", "no"):
                self._record_reasoning(model_id, without, param_ok=False)
            return without
        if with_param in ("yes", "no"):
            self._record_reasoning(model_id, with_param, param_ok=True)
        return with_param

    def _probe_once(self, client: object, model_id: str, reasoning_effort: str | None) -> str:
        """单次探测：返回 "yes"/"no"/"rejected"（参数被拒）/"unknown"（其他失败）。"""
        options = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        try:
            stream = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "1+1=?"}],
                max_tokens=64,  # 探测成本上限（用户裁决：单次 50–100 token 内）
                stream=True,
                timeout=CONNECT_TIMEOUT_S,
                **options,
            )
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            return "rejected" if ("BadRequest" in name or "Unprocessable" in name) else "unknown"
        saw_reasoning = False
        try:
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                if delta is not None and _reasoning_text(delta):
                    saw_reasoning = True
                    break
        except Exception:  # noqa: BLE001
            return "unknown"
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    log.debug("关闭探测流失败", exc_info=True)
        return "yes" if saw_reasoning else "no"

    # -- 标题提炼（rev59）---------------------------------------------------
    def generate_title(self, messages: list[dict], model_id: str) -> str:
        """模型提炼单行标题（非流式短输出）：返回空串或调用异常 → 调用方走截断回退。

        不发 `max_tokens`（承 rev20）；取 `message.content` 尾部，供调用方统一软截断。
        """
        provider = self._find_provider_for_model(model_id)
        if provider is None:
            return ""
        try:
            self._ensure_secure_transport(provider.base_url)
            if not self.whitelist.is_allowed(provider.base_url):
                return ""
            api_key = self._api_key_for(provider)
            if not api_key:
                return ""
        except GatewayError:
            return ""
        client = self._client(provider.base_url, api_key)
        options = self._reasoning_options(model_id)
        try:
            completion = client.chat.completions.create(
                model=model_id,
                messages=messages,
                stream=False,
                timeout=self.silent_timeout,
                **options,
            )
        except Exception:  # noqa: BLE001 - 标题提炼失败不阻断对话，静默回退
            log.debug("标题提炼失败，走截断回退：%s", model_id, exc_info=True)
            return ""
        choices = getattr(completion, "choices", None)
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        return (content or "").strip()

    def get_slots(self) -> dict[str, str | None]:
        return dict(self.models.slots)

    def set_slot(self, slot: str, model_id: str | None) -> None:
        if slot not in self.models.slots:
            raise GatewayProtocolError(f"未知槽位：{slot}")
        self.models.slots[slot] = model_id  # type: ignore[index]
        self.store.save("models", self.models)

    # -- 配置写入 ----------------------------------------------------------
    def upsert_provider(self, spec: ProviderSpec, api_key: str | None) -> ProviderSpec:
        self._ensure_secure_transport(spec.base_url)  # 先校验后落盘（rev15）
        existing = self._find_provider(spec.id)
        if existing is None:
            pc = ProviderConfig(id=spec.id, name=spec.name, base_url=spec.base_url)
            self.models.providers.append(pc)
        else:
            pc = existing
        pc.name = spec.name
        pc.base_url = spec.base_url
        pc.local = spec.local
        # rev25：思考字段按 id 合并 —— 探测缓存（detected / param_ok）是端点事实，
        # 编辑模型表时不得被清空；用户偏好（reasoning）以本次提交为准。
        old_models = {m.id: m for m in pc.models}
        pc.models = []
        for m in spec.models:
            old = old_models.get(m.id)
            pc.models.append(
                ModelConfig(
                    id=m.id,
                    ctx_window=m.ctx_window,
                    tags=list(m.tags),
                    reasoning=m.reasoning,
                    reasoning_detected=old.reasoning_detected if old else "unknown",
                    reasoning_param_ok=old.reasoning_param_ok if old else False,
                )
            )
        if api_key and not pc.local:
            self.secrets.set(secret_name(spec.id), api_key)
            pc.key_ref = key_ref_for(spec.id)
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
        self.secrets.delete(secret_name(provider_id))
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
        try:
            self._ensure_secure_transport(provider.base_url)
        except GatewayBlocked as exc:
            return False, None, exc.code  # 契约：探测类出口返回码，不抛
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
        try:
            self._ensure_secure_transport(provider.base_url)
        except GatewayBlocked as exc:
            return False, [], exc.code  # 契约：探测类出口返回码，不抛
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
        on_reasoning: Callable[[str], None] | None = None,
        params: SessionParams | None = None,
        tools: list[dict] | None = None,
        on_tool_calls: Callable[[list[dict]], None] | None = None,
    ) -> Usage:
        provider = self._find_provider_for_model(model_id)
        if provider is None:
            raise GatewayProtocolError(
                f"该模型在供应商不可用：{model_id} 不属于任何已配置的供应商",
                code="model_not_found",
            )
        self._ensure_secure_transport(provider.base_url)
        if not self.whitelist.is_allowed(provider.base_url):
            raise GatewayBlocked("已被网络白名单拦截：该供应商地址不在允许列表内")
        api_key = self._api_key_for(provider)
        if not api_key:
            raise GatewayAuthError("凭据不可用", code="key_missing")
        client = self._client(provider.base_url, api_key)

        effective_tools = None if (tools and self._tools_unsupported(model_id)) else tools
        usage = self._stream_with_retry(
            client, model_id, messages, cancel_token, on_delta, on_reasoning, params,
            effective_tools, on_tool_calls,
        )
        self.meter.add(usage)
        return usage

    def _tools_unsupported(self, model_id: str) -> bool:
        m = self._find_model(model_id)
        return bool(m is not None and m.tools_unsupported)

    def _record_tools_unsupported(self, model_id: str) -> None:
        """被动判定：该端点不接受 function calling，落盘标记（幂等）。"""
        m = self._find_model(model_id)
        if m is None or m.tools_unsupported:
            return
        m.tools_unsupported = True
        self.store.save("models", self.models)

    def _stream_with_retry(
        self,
        client: object,
        model_id: str,
        messages: list[dict],
        cancel_token: CancelTokenLike | None,
        on_delta: Callable[[str], None],
        on_reasoning: Callable[[str], None] | None = None,
        params: SessionParams | None = None,
        tools: list[dict] | None = None,
        on_tool_calls: Callable[[list[dict]], None] | None = None,
    ) -> Usage:
        last_exc: GatewayError | None = None
        effective_tools = tools
        tools_retried = False
        for attempt in range(2):  # 初次 + 至多 1 次重试
            holder = {"got_delta": False, "tool_calls": [], "tools_rejected": False}
            try:
                usage = self._stream_once(
                    client, model_id, messages, cancel_token, on_delta, holder,
                    on_reasoning, params, effective_tools,
                )
                if on_tool_calls is not None and holder["tool_calls"]:
                    on_tool_calls(holder["tool_calls"])
                return usage
            except GatewayTimeout:
                raise
            except Exception as exc:  # noqa: BLE001
                if holder.get("tools_rejected") and effective_tools is not None and not tools_retried:
                    # 端点因 tools 返回 400：判定为不支持 function calling，撤工具重试一次并标记。
                    tools_retried = True
                    effective_tools = None
                    self._record_tools_unsupported(model_id)
                    log.info("模型不接受 function calling，撤工具重试一次：%s", model_id)
                    continue
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
        on_reasoning: Callable[[str], None] | None = None,
        params: SessionParams | None = None,
        tools: list[dict] | None = None,
    ) -> Usage:
        """单次流式调用。

        静默超时是**双重保险**：
        1. `timeout=` 交给 SDK —— 它约束「两次数据之间的等待」，故真·静默（一个 chunk 都不来）
           也会在阈值处中止；否则循环里的判定永远等不到下一次迭代，取消同样无法生效（spec rev8 §1）。
        2. 循环内判定 —— 兜底自定义客户端（如测试替身）忽略 `timeout` 的情形。

        rev25：正文与思考分流（`reasoning_content`）；记录首 token / 流结束时刻，供平均 TPS。
        v0.0.3：`tools` 走 native function calling；流式聚合 `tool_calls` 增量写入 holder。
        """
        options = {**_param_options(params), **self._reasoning_options(model_id)}
        if tools:
            options["tools"] = tools
            options["tool_choice"] = "auto"
        try:
            stream = client.chat.completions.create(
                model=model_id,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                timeout=self.silent_timeout,
                **options,
            )
        except Exception as exc:  # noqa: BLE001 - 400 可能因 tools 不被接受，交由上层撤工具重试
            name = type(exc).__name__
            if tools is not None and ("BadRequest" in name or "Unprocessable" in name):
                holder["tools_rejected"] = True
            raise
        prompt = completion = total = 0
        started = time.monotonic()
        first_token: float | None = None
        last = started
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
                    reasoning = _reasoning_text(delta)
                    content = getattr(delta, "content", None) if delta is not None else None
                    if reasoning:
                        if first_token is None:
                            first_token = now
                        holder["got_delta"] = True
                        if on_reasoning is not None:
                            on_reasoning(reasoning)
                    if content:
                        if first_token is None:
                            first_token = now
                        on_delta(content)
                        holder["got_delta"] = True
                    tool_deltas = getattr(delta, "tool_calls", None) if delta is not None else None
                    if tool_deltas:
                        if first_token is None:
                            first_token = now
                        holder["got_delta"] = True
                        _merge_tool_calls(holder["tool_calls"], tool_deltas)
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
        elapsed_ms = max(0, int((last - started) * 1000))
        first_ms = max(0, int((first_token - started) * 1000)) if first_token is not None else 0
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total or (prompt + completion),
            elapsed_ms=elapsed_ms,
            first_token_ms=first_ms,
        )
