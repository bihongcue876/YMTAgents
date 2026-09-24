"""消耗 DPIM 结构化输入/提示词的可脚本化网关替身。"""

from __future__ import annotations

import json

from shared.envelope import Usage
from tests.mocks.gateway import MockGateway


class DpimGateway(MockGateway):
    def __init__(self) -> None:
        super().__init__(chunks=())
        self.agent_calls: list[tuple[str, dict]] = []

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
    ):
        prompt = str(messages[0]["content"])
        payload = json.loads(messages[1]["content"])
        self.agent_calls.append((prompt, payload))
        if "Core 角色" in prompt:
            response = {"summary": "YMT memory store", "intent": "record architecture", "keywords": ["YMT"]}
        elif "Infomater 角色" in prompt:
            source = str(payload.get("content") or "")
            response = {
                "items": [
                    {
                        "title": "YMT memory",
                        "content": source[:500] or "YMT memory store",
                        "node_type": "data",
                        "confidence": 0.9,
                    }
                ]
            }
        elif "Grapher 角色" in prompt:
            response = {"edges": []}
        else:
            response = {"accepted_titles": ["YMT memory"], "accepted_edges": []}
        on_delta(json.dumps(response, ensure_ascii=False))
        return Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
