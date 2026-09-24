"""副思考链引擎单元测试（切片 1）：四形态 / 终止原因 / 降级 / 超时部分结果 / 计量 / 思考流。

替身 `BtcmMockGateway` 按 system 提示词识别 Agent 并返回结构化输出（消费被测数据）。
"""

from __future__ import annotations

import pytest

from core.modules.btcm import engine as engine_mod
from core.modules.btcm.config import (
    agent_params,
    effort_directive,
    load_prompt,
    resolve_model,
)
from core.modules.btcm.engine import BtcmCancelled, BtcmEngine, BtcmError
from core.modules.btcm.parse import AgentOutputError, parse_json_object, require_keys
from shared.schema import BtcmAgentParams, BtcmConfig
from tests.mocks.btcm_gateway import BtcmMockGateway, Clock


def _engine(gateway, config: BtcmConfig | None = None, **kwargs):
    return BtcmEngine(
        gateway,
        config or BtcmConfig(),
        session_id="ses_1",
        turn_seq=1,
        model_id="mock-model",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# parse / config（纯函数）
# ---------------------------------------------------------------------------
def test_parse_json_object_accepts_fence_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('前言 {"a": 2} 后记') == {"a": 2}


def test_parse_json_object_rejects_non_object():
    with pytest.raises(AgentOutputError):
        parse_json_object("没有 JSON")
    with pytest.raises(AgentOutputError):
        parse_json_object("[1, 2]")


def test_require_keys_reports_missing():
    with pytest.raises(AgentOutputError):
        require_keys({"a": 1}, ["a", "b"], "creative")


def test_agent_params_defaults_then_override():
    config = BtcmConfig()
    assert agent_params(config, "creative")["temperature"] == 0.8
    assert agent_params(config, "creative")["num_candidates"] == 3
    config.agents = {"creative": BtcmAgentParams(temperature=0.5)}
    params = agent_params(config, "creative")
    assert params["temperature"] == 0.5
    assert params["num_candidates"] == 3  # 未覆盖项保留默认


def test_effort_directive_only_for_light_and_deep():
    assert "略想" in effort_directive("light")
    assert "深层" in effort_directive("deep")
    assert effort_directive("standard") == ""


def test_resolve_model_prefers_session_then_slot_then_main():
    class _Gateway:
        def get_slots(self):
            return {"main": "m-main", "thinking": "m-think"}

    gateway = _Gateway()
    assert resolve_model(gateway, "m-session", "thinking") == "m-session"
    assert resolve_model(gateway, None, "thinking") == "m-think"
    assert resolve_model(gateway, None, "fast") == "m-main"
    assert resolve_model(gateway, None, "thinking") != ""


def test_prompts_are_files_not_constants():
    for name in ("creative", "validator", "controller", "controller_finalize", "meta"):
        assert load_prompt(name).strip()


# ---------------------------------------------------------------------------
# 四形态与终止原因
# ---------------------------------------------------------------------------
def test_hybrid_stops_on_validation_passed():
    gateway = BtcmMockGateway(verdicts=["pass"])
    emitted: list = []
    data = _engine(gateway, emit=emitted.append).run(question="q")
    assert data["verdict"] == "pass"
    assert data["termination_reason"] == "validation_passed"
    assert data["iterations_used"] == 1
    assert gateway.agent_calls == ["creative", "validator", "meta"]
    # meta log_intermediate 默认开 → 中间日志带 meta_reflection 键
    assert data["intermediate_log"][0]["meta_reflection"]["decision"] == "stop"
    assert [e.type for e in emitted].count("think.iteration") == 1


def test_hybrid_runs_to_max_iterations():
    gateway = BtcmMockGateway(verdicts=["fail", "fail"], decisions=["continue", "continue"])
    engine = _engine(gateway, BtcmConfig(max_iterations=2))
    data = engine.run(question="q")
    assert data["iterations_used"] == 2
    assert data["termination_reason"] == "max_iterations"
    assert gateway.agent_calls.count("creative") == 2


def test_hybrid_controller_stop_on_conditional_pass():
    gateway = BtcmMockGateway(verdicts=["conditional_pass"], decisions=["stop"])
    data = _engine(gateway).run(question="q")
    assert data["termination_reason"] == "controller_stop"


def test_light_effort_forces_single_round():
    gateway = BtcmMockGateway(verdicts=["fail", "fail"], decisions=["continue", "continue"])
    engine = _engine(gateway, BtcmConfig(max_iterations=3))
    data = engine.run(question="q", effort="light")
    assert data["iterations_used"] == 1
    assert gateway.agent_calls.count("creative") == 1


def test_pure_creative_single_pass():
    gateway = BtcmMockGateway()
    data = _engine(gateway).run(question="q", mode="creative")
    assert data["termination_reason"] == "single_pass"
    assert data["candidates"] == ["候选甲（理由）", "候选乙（理由）"]
    assert gateway.agent_calls == ["creative"]


def test_pure_validation_requires_candidate():
    with pytest.raises(BtcmError) as excinfo:
        _engine(BtcmMockGateway()).run(question="q", mode="validate")
    assert excinfo.value.code == "invalid"


def test_pure_validation_reports_verdict():
    gateway = BtcmMockGateway(verdicts=["conditional_pass"])
    data = _engine(gateway).run(question="q", mode="validate", candidate="丙")
    assert data["verdict"] == "conditional_pass"
    assert data["termination_reason"] == "single_pass"
    assert "最优候选" in data["conclusion"]


def test_long_chain_iterations_then_finalize():
    gateway = BtcmMockGateway()
    engine = _engine(gateway, BtcmConfig(max_iterations=2))
    data = engine.run(question="q", mode="long")
    assert gateway.agent_calls == ["controller", "controller", "controller_finalize"]
    assert data["conclusion"] == "最终结论"
    assert [row["iteration"] for row in data["intermediate_log"]] == [1, 2]


# ---------------------------------------------------------------------------
# 降级 / 失败 / 超时
# ---------------------------------------------------------------------------
def test_validator_failure_degrades_to_fail_verdict():
    gateway = BtcmMockGateway(fail_agents=("validator",), decisions=["stop"])
    data = _engine(gateway).run(question="q")
    assert data["verdict"] == "fail"
    assert any("验证 Agent 失败" in issue for issue in data["issues"])


def test_non_validator_failure_is_internal_error():
    gateway = BtcmMockGateway(fail_agents=("creative",))
    with pytest.raises(BtcmError) as excinfo:
        _engine(gateway).run(question="q")
    assert excinfo.value.code == "internal"


def test_timeout_returns_partial_result(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(engine_mod.time, "monotonic", clock)
    gateway = BtcmMockGateway(
        verdicts=["fail", "fail"],
        decisions=["continue", "continue"],
        clock=clock,
        advance_on=("creative", 5.0),
        advance_on_occurrence=2,  # 第 1 轮完整跑完，第 2 轮开局越界 → 带部分结果返回
    )
    engine = _engine(gateway, BtcmConfig(max_iterations=3, timeout=1))
    data = engine.run(question="q")
    assert data["termination_reason"] == "timeout"
    assert data["iterations_used"] == 1
    assert data["verdict"] == "fail"


def test_timeout_before_any_round_is_error(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(engine_mod.time, "monotonic", clock)
    gateway = BtcmMockGateway(clock=clock, advance_on=("creative", 5.0))
    engine = _engine(gateway, BtcmConfig(timeout=1))
    with pytest.raises(BtcmError) as excinfo:
        engine.run(question="q")
    assert excinfo.value.code == "timeout"


def test_cancel_token_aborts_run():
    class _Cancelled:
        def is_cancelled(self) -> bool:
            return True

    with pytest.raises(BtcmCancelled):
        _engine(BtcmMockGateway(), cancel=_Cancelled()).run(question="q")


# ---------------------------------------------------------------------------
# 计量 / 思考流 / 参数
# ---------------------------------------------------------------------------
def test_usage_accumulates_across_agents():
    gateway = BtcmMockGateway(verdicts=["pass"])
    data = _engine(gateway).run(question="q")
    usage = data["usage"]
    # creative + validator + meta = 3 次调用，每次 7/3/10
    assert usage["prompt_tokens"] == 21
    assert usage["completion_tokens"] == 9
    assert usage["total_tokens"] == 30
    assert usage["elapsed_ms"] == 36
    assert usage["first_token_ms"] == 12


def test_think_delta_and_reasoning_streams_are_separated():
    gateway = BtcmMockGateway(verdicts=["pass"])
    emitted: list = []
    _engine(gateway, emit=emitted.append).run(question="q")
    deltas = [e for e in emitted if e.type == "think.delta"]
    assert {d.kind for d in deltas} == {"content", "reasoning"}
    assert {d.agent for d in deltas} == {"creative", "validator", "meta"}
    assert all(d.think_id for d in deltas)


def test_temperature_is_sent_per_agent():
    gateway = BtcmMockGateway(verdicts=["pass"])
    _engine(gateway).run(question="q")
    by_agent = {c["agent"]: p for c, p in zip(gateway.calls, gateway.agent_params, strict=True)}
    assert by_agent["creative"].temperature == 0.8
    assert by_agent["validator"].temperature == 0.3