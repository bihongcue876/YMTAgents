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
    ) -> None:
        self._chunks = list(chunks)
        self._slots = slots if slots is not None else {"main": model_id}
        self._model_id = model_id
        self._ctx_window = ctx_window
        self._cancel_after = cancel_after
        self._timeout = timeout
        self._fail = fail
        self._test_result = test_result
        self.calls: list[dict] = []
        self._providers: list[ProviderSpec] = []

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

    def test_connection(self, provider_id: str, model_id: str) -> tuple[bool, int | None, str | None]:
        self.calls.append({"test_connection": (provider_id, model_id)})
        return self._test_result

    def stream_chat(self, session_id, turn_seq, model_id, messages, cancel_token, on_delta) -> Usage:
        self.calls.append({"model_id": model_id, "messages": messages, "turn_seq": turn_seq})
        if self._timeout:
            raise GatewayTimeout("mock 静默超时")
        if self._fail is not None:
            raise self._fail
        for i, chunk in enumerate(self._chunks):
            on_delta(chunk)
            if self._cancel_after is not None and i == self._cancel_after and cancel_token:
                cancel_token.cancel()
        return Usage(prompt_tokens=5, completion_tokens=len(self._chunks), total_tokens=5 + len(self._chunks))
