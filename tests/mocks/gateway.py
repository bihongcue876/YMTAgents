"""可脚本化 MockGateway（spec §8）。

用于 GUI 无网联调与集成测试：可控流式输出 / 超时 / 失败 / 取消。
"""

from __future__ import annotations

from shared.envelope import ModelSpec, ProviderSpec, Usage
from core.gateway.errors import GatewayTimeout
from core.gateway.provider import IModelGateway


class MockGateway(IModelGateway):
    def __init__(
        self,
        chunks=("你", "好"),
        slots: dict | None = None,
        model_id: str = "mock-model",
        ctx_window: int = 8192,
        cancel_after: int | None = None,
        timeout: bool = False,
        fail: Exception | None = None,
        test_result: tuple[bool, int | None, str | None] = (True, 12, None),
        remote_models: tuple[bool, list[str], str | None] = (True, ["mock-model"], None),
        tool_call_rounds: list[list[dict]] | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._slots = slots if slots is not None else {"main": model_id}
        self._model_id = model_id
        self._ctx_window = ctx_window
        self._cancel_after = cancel_after
        self._timeout = timeout
        self._fail = fail
        self._test_result = test_result
        self._remote_models = remote_models
        #: rev42：按轮次脚本的 tool_calls（每项形如 {"id","name","arguments"}）。
        self._tool_call_rounds = [list(r) for r in (tool_call_rounds or [])]
        self._tool_round_index = 0
        self.calls: list[dict] = []
        self._providers: list[ProviderSpec] = []
        self._auto_title = ""  # rev59：自动标题返回；空 → 调用方走首条消息截断回退

    def list_providers(self) -> list[ProviderSpec]:
        if self._providers:
            return list(self._providers)
        return [
            ProviderSpec(
                id="prv_mock",
                name="Mock",
                base_url="https://mock.local",
                models=[ModelSpec(id=self._model_id, ctx_window=self._ctx_window)],
                key_status="stored",
            )
        ]

    def upsert_provider(self, spec: ProviderSpec, api_key: str | None) -> ProviderSpec:
        self._providers = [p for p in self._providers if p.id != spec.id]
        stored = spec.model_copy(update={"key_status": "stored" if api_key else spec.key_status})
        self._providers.append(stored)
        return stored

    def toggle_provider(self, provider_id: str, enabled: bool) -> bool:
        """rev68：可脚本化替身同步启停（未知供应商返回 False）。"""
        for i, p in enumerate(self._providers):
            if p.id == provider_id:
                self._providers[i] = p.model_copy(update={"enabled": bool(enabled)})
                return True
        return False

    def delete_provider(self, provider_id: str) -> bool:
        before = len(self._providers)
        self._providers = [p for p in self._providers if p.id != provider_id]
        return len(self._providers) != before

    def get_slots(self) -> dict[str, str | None]:
        return dict(self._slots)

    def set_slot(self, slot: str, model_id: str | None) -> None:
        self._slots[slot] = model_id

    def reload_settings(self) -> None:
        """Mock 不持外部配置，空实现（契约要求，见 IModelGateway）。"""

    def reasoning_pending(self, model_id: str) -> bool:
        """Mock 默认视为「已确定、不支持思考」，避免测试里触发探测（rev25）。"""
        return False

    def probe_reasoning(self, model_id: str) -> str:
        self.calls.append({"probe_reasoning": model_id})
        return "no"

    def generate_title(self, messages: list[dict], model_id: str) -> str:
        self.calls.append({"generate_title": model_id})
        return self._auto_title

    def test_connection(self, provider_id: str, model_id: str) -> tuple[bool, int | None, str | None]:
        self.calls.append({"test_connection": (provider_id, model_id)})
        return self._test_result

    def list_remote_models(self, provider_id: str) -> tuple[bool, list[str], str | None]:
        self.calls.append({"list_remote_models": provider_id})
        return self._remote_models

    def stream_chat(
        self,
        session_id,
        turn_seq,
        model_id,
        messages,
        cancel_token,
        on_delta,
        on_reasoning=None,
        params=None,
        tools=None,
        on_tool_calls=None,
    ) -> Usage:
        self.calls.append(
            {
                "model_id": model_id,
                "messages": messages,
                "turn_seq": turn_seq,
                "params": params,
                "tools": tools,
            }
        )
        if self._timeout:
            raise GatewayTimeout("mock 静默超时")
        if self._fail is not None:
            raise self._fail
        # rev42：按轮次脚本返回一次 tool_calls（不再吐正文）。
        if self._tool_round_index < len(self._tool_call_rounds):
            script = self._tool_call_rounds[self._tool_round_index]
            self._tool_round_index += 1
            if on_tool_calls is not None:
                on_tool_calls(
                    [
                        {
                            "id": item.get("id", f"call_{self._tool_round_index}"),
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }
                        for item in script
                    ]
                )
            return Usage(prompt_tokens=5, completion_tokens=1, total_tokens=6)
        for i, chunk in enumerate(self._chunks):
            if on_reasoning is not None and isinstance(chunk, tuple):
                text, reasoning = chunk
                (on_reasoning if reasoning else on_delta)(text)
            else:
                on_delta(chunk)
            if self._cancel_after is not None and i == self._cancel_after and cancel_token:
                cancel_token.cancel()
        return Usage(prompt_tokens=5, completion_tokens=len(self._chunks), total_tokens=5 + len(self._chunks))
