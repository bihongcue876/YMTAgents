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
    ) -> None:
        self._chunks = list(chunks)
        self._slots = slots if slots is not None else {"main": model_id}
        self._model_id = model_id
        self._ctx_window = ctx_window
        self._cancel_after = cancel_after
        self._timeout = timeout
        self._fail = fail
        self.calls: list[dict] = []

    def list_providers(self) -> list[ProviderSpec]:
        return [
            ProviderSpec(
                id="prv_mock",
                name="Mock",
                base_url="https://mock.local",
                models=[ModelSpec(id=self._model_id, ctx_window=self._ctx_window)],
                key_status="stored",
            )
        ]

    def get_slots(self) -> dict[str, str | None]:
        return dict(self._slots)

    def set_slot(self, slot: str, model_id: str | None) -> None:
        self._slots[slot] = model_id

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
