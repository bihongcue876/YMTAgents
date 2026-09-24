"""BTCM 脚本化网关替身。

一个替身承担两件事：普通对话回合（承 `MockGateway`）与四个 BTCM Agent 的结构化输出。
按 messages 的 **system 提示词** 识别当前是哪个 Agent —— 替身必须消费被测数据
（rev8 教训：不消费数据的替身会让断言失真）。
"""

from __future__ import annotations

import json
from typing import Any

from shared.envelope import Usage
from tests.mocks.gateway import MockGateway

#: 识别标记按「越具体越先匹配」排序（controller_finalize 与 controller 共用总控提示词）。
_AGENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("controller_finalize", "多轮长链思考"),
    ("creative", "创意生成 Agent"),
    ("validator", "验证 Agent"),
    ("meta", "meta Agent"),
    ("controller", "总控 Agent"),
)


class Clock:
    """可推进的假时钟（超时用例用；不改 stdlib 全局行为）。"""

    def __init__(self, start: float = 100.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class BtcmMockGateway(MockGateway):
    def __init__(
        self,
        *,
        verdicts: list[str] | None = None,
        decisions: list[str] | None = None,
        candidates: list[str] | None = None,
        fail_agents: tuple[str, ...] = (),
        clock: Clock | None = None,
        advance_on: tuple[str, float] | None = None,
        advance_on_occurrence: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._verdicts = list(verdicts or ["pass"])
        self._decisions = list(decisions or ["stop"])
        self._candidates = list(candidates or ["候选甲（理由）", "候选乙（理由）"])
        self._fail_agents = tuple(fail_agents)
        self._clock = clock
        self._advance_on = advance_on
        self._advance_on_occurrence = int(advance_on_occurrence)
        self._agent_seen: dict[str, int] = {}
        self.agent_calls: list[str] = []
        self.agent_params: list[Any] = []

    # -- 识别 --------------------------------------------------------------
    @staticmethod
    def agent_of(messages: list[dict]) -> str | None:
        system = ""
        for message in messages:
            if message.get("role") == "system":
                system = str(message.get("content") or "")
                break
        for agent, marker in _AGENT_MARKERS:
            if marker in system:
                return agent
        return None

    def _payload(self, agent: str) -> str:
        if agent == "creative":
            return json.dumps(
                {"candidates": self._candidates, "conclusion": "综合全部候选的推荐说明"},
                ensure_ascii=False,
            )
        if agent == "validator":
            verdict = self._verdicts.pop(0) if self._verdicts else "pass"
            return json.dumps(
                {
                    "verdict": verdict,
                    "best_candidate": self._candidates[0] if self._candidates else None,
                    "issues": [] if verdict == "pass" else ["事实存疑"],
                    "suggestions": [] if verdict == "pass" else ["补充出处"],
                    "next_actions": ["采纳最优候选"],
                },
                ensure_ascii=False,
            )
        if agent == "meta":
            decision = self._decisions.pop(0) if self._decisions else "stop"
            return json.dumps(
                {"conclusion": "总体认识", "remaining_issues": [], "next_direction": "", "decision": decision},
                ensure_ascii=False,
            )
        if agent == "controller_finalize":
            return json.dumps({"conclusion": "最终结论"}, ensure_ascii=False)
        return json.dumps({"thought": f"要点 {len(self.agent_calls)}"}, ensure_ascii=False)

    # -- 流式 --------------------------------------------------------------
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
        agent = self.agent_of(messages)
        if agent is None:
            return super().stream_chat(
                session_id,
                turn_seq,
                model_id,
                messages,
                cancel_token,
                on_delta,
                on_reasoning=on_reasoning,
                params=params,
                tools=tools,
                on_tool_calls=on_tool_calls,
            )
        self.agent_calls.append(agent)
        self.agent_params.append(params)
        self._agent_seen[agent] = self._agent_seen.get(agent, 0) + 1
        self.calls.append({"agent": agent, "messages": messages, "model_id": model_id})
        if agent in self._fail_agents:
            raise RuntimeError(f"mock {agent} 失败")
        if on_reasoning is not None and agent == "validator":
            on_reasoning("核对候选……")
        on_delta(self._payload(agent))
        if (
            self._advance_on is not None
            and agent == self._advance_on[0]
            and self._agent_seen[agent] == self._advance_on_occurrence
            and self._clock is not None
        ):
            self._clock.advance(self._advance_on[1])
        return Usage(
            prompt_tokens=7,
            completion_tokens=3,
            total_tokens=10,
            elapsed_ms=12,
            first_token_ms=4,
        )