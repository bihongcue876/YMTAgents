"""副思考链引擎（切片 1，承原型 `core/loop.py` 语义，**同步重写**）。

- 四形态由 `enable_creative × enable_validator` 决定：混合 / 纯创造 / 纯验证 / 长链。
- 终止原因：`validation_passed` / `controller_stop` / `max_iterations` / `timeout` / `single_pass`。
- 降级：validator 唯一允许 fail-degrade；其余失败整次失败（回 `tool_backend_error`）。
- 全局超时：以**取消令牌 + 截止时间**实现（`_DeadlineToken`）；已完成 ≥1 轮则带部分结果返回。
- 独立计量：各次调用 `Usage` 累加，交执行器回填。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from shared.envelope import SessionParams, ThinkDelta, ThinkIteration

from core.modules.btcm.config import agent_params, effort_directive, load_prompt
from core.modules.btcm.parse import AgentOutputError, parse_json_object, require_keys

RETRY_BACKOFF_SECONDS = 0.5
VERDICTS = ("pass", "conditional_pass", "fail")


class BtcmError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BtcmCancelled(BtcmError):
    def __init__(self, message: str = "已取消") -> None:
        super().__init__("cancelled", message)


class BtcmTimeout(BtcmError):
    def __init__(self, message: str = "整体超时") -> None:
        super().__init__("timeout", message)


class _DeadlineToken:
    """组合取消令牌：用户取消优先，其次全局截止时间；供网关逐块轮询。"""

    def __init__(self, deadline: float, inner: object | None) -> None:
        self._deadline = deadline
        self._inner = inner

    def is_cancelled(self) -> bool:
        if self._inner is not None and getattr(self._inner, "is_cancelled", lambda: False)():
            return True
        return time.monotonic() >= self._deadline


def _fail_report(reason: str) -> dict:
    return {
        "verdict": "fail",
        "best_candidate": None,
        "issues": [reason],
        "suggestions": [],
        "next_actions": [],
    }


def _history_lines(previous: list[str]) -> str:
    keep = 5
    total = len(previous)
    if total <= keep + 1:
        selected, omitted = previous, 0
    else:
        selected = [previous[0]] + previous[-keep:]
        omitted = total - len(selected)
    lines = [f"- {t}" for t in selected]
    if omitted:
        lines.insert(1, f"……（中间 {omitted} 条要点已省略）")
    return "\n".join(lines)


class BtcmEngine:
    def __init__(
        self,
        gateway: Any,
        config: Any,
        *,
        session_id: str,
        turn_seq: int,
        model_id: str,
        emit: Callable[[Any], None] | None = None,
        cancel: object | None = None,
        think_id: str = "",
    ) -> None:
        self._gateway = gateway
        self._config = config
        self._session_id = session_id
        self._turn_seq = turn_seq
        self._model_id = model_id
        self._emit = emit or (lambda _e: None)
        self._cancel = cancel
        self._think_id = think_id or model_id
        self._effort = "standard"
        self._deadline = time.monotonic()
        self._token: _DeadlineToken | None = None
        self._usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "elapsed_ms": 0,
            "first_token_ms": 0,
        }
        names = ("creative", "validator", "controller", "controller_finalize", "meta")
        self._prompts = {name: load_prompt(name) for name in names}

    # ---------- 入口 ----------
    def run(
        self,
        *,
        question: str,
        effort: str = "standard",
        candidate: str | None = None,
        context: str | None = None,
        mode: str = "auto",
    ) -> dict:
        self._effort = effort or "standard"
        timeout = max(1, int(self._config.timeout or 3600))
        self._deadline = time.monotonic() + timeout
        self._token = _DeadlineToken(self._deadline, self._cancel)

        enable_creative = bool(self._config.enable_creative)
        enable_validator = bool(self._config.enable_validator)
        if mode == "creative":
            enable_creative, enable_validator = True, False
        elif mode == "validate":
            enable_creative, enable_validator = False, True
        elif mode == "long":
            enable_creative, enable_validator = False, False

        try:
            if enable_creative and enable_validator:
                data = self._hybrid(question, candidate, context)
            elif enable_creative:
                data = self._pure_creative(question, candidate, context)
            elif enable_validator:
                data = self._pure_validation(question, candidate, context)
            else:
                data = self._long_chain(question, context)
        except (BtcmCancelled, BtcmTimeout, BtcmError):
            raise
        except Exception as exc:  # noqa: BLE001 - 未预期异常统一转内部错误
            raise BtcmError("internal", f"内部错误：{type(exc).__name__}: {exc}") from exc
        data["usage"] = dict(self._usage)
        return data

    # ---------- 四形态 ----------
    def _hybrid(self, question: str, candidate: str | None, context: str | None) -> dict:
        max_iterations = max(1, int(self._config.max_iterations or 2))
        if self._effort == "light":
            max_iterations = 1
        log_intermediate = bool(agent_params(self._config, "meta").get("log_intermediate", True))

        current_candidate = candidate
        validation_feedback: dict | None = None
        next_direction: str | None = None
        intermediate_log: list[dict] = []
        iterations = 0
        final_report: dict | None = None
        final_reflection: dict | None = None
        termination = "max_iterations"
        timed_out = False
        try:
            for iteration in range(1, max_iterations + 1):
                self._check()
                gen = self._run_agent(
                    "creative",
                    self._creative_messages(
                        question, context, current_candidate, validation_feedback,
                        next_direction, is_final=(iteration == max_iterations),
                    ),
                    self._temp("creative"),
                    self._parse_creative,
                )
                candidates = gen["candidates"]
                report = self._validator(question, context, candidates)
                reflection = self._run_agent(
                    "meta",
                    self._meta_messages(
                        question, context, iteration, candidates, report,
                        max_iterations - iteration,
                    ),
                    self._temp("meta"),
                    self._parse_meta,
                )
                iterations = iteration
                final_report, final_reflection = report, reflection
                if log_intermediate:
                    intermediate_log.append(
                        {
                            "iteration": iteration,
                            "creative_output": candidates,
                            "validator_output": {
                                "verdict": report["verdict"],
                                "issues": report["issues"],
                            },
                            "meta_reflection": {
                                "decision": reflection["decision"],
                                "next_direction": reflection.get("next_direction", ""),
                                "remaining_iterations": max_iterations - iteration,
                            },
                        }
                    )
                self._emit(
                    ThinkIteration(
                        think_id=self._think_id,
                        iteration=iteration,
                        verdict=report["verdict"],
                        decision=reflection["decision"],
                    )
                )
                if report["verdict"] == "pass":
                    termination = "validation_passed"
                    break
                if reflection["decision"] == "stop" and report["verdict"] != "fail":
                    termination = "controller_stop"
                    break
                current_candidate = report.get("best_candidate") or candidates[0]
                validation_feedback = report
                next_direction = reflection.get("next_direction") or None
        except BtcmTimeout:
            if iterations == 0:
                raise BtcmError("timeout", "整体超时且未完成任何一轮") from None
            timed_out = True

        if final_report is None or final_reflection is None:
            raise BtcmError("internal", "循环异常终止且无可用轮次结果")
        if timed_out:
            termination = "timeout"
        data: dict = {
            "verdict": final_report["verdict"],
            "conclusion": final_reflection.get("conclusion", ""),
            "issues": final_report["issues"],
            "suggestions": final_report["suggestions"],
            "next_actions": final_report["next_actions"],
            "iterations_used": iterations,
            "termination_reason": termination,
        }
        if log_intermediate:
            data["intermediate_log"] = intermediate_log
        return data

    def _pure_creative(self, question: str, candidate: str | None, context: str | None) -> dict:
        gen = self._run_agent(
            "creative",
            self._creative_messages(question, context, candidate, None, None, is_final=False),
            self._temp("creative"),
            self._parse_creative,
        )
        self._emit(ThinkIteration(think_id=self._think_id, iteration=1))
        return {
            "candidates": gen["candidates"],
            "conclusion": gen["conclusion"],
            "iterations_used": 1,
            "termination_reason": "single_pass",
        }

    def _pure_validation(self, question: str, candidate: str | None, context: str | None) -> dict:
        if not candidate:
            raise BtcmError("invalid", "纯验证形态下 candidate 必填")
        report = self._validator(question, context, [candidate])
        self._emit(
            ThinkIteration(think_id=self._think_id, iteration=1, verdict=report["verdict"])
        )
        return {
            "verdict": report["verdict"],
            "conclusion": self._validation_conclusion(report),
            "issues": report["issues"],
            "suggestions": report["suggestions"],
            "next_actions": report["next_actions"],
            "iterations_used": 1,
            "termination_reason": "single_pass",
        }

    def _long_chain(self, question: str, context: str | None) -> dict:
        max_iterations = max(1, int(self._config.max_iterations or 2))
        if self._effort == "light":
            max_iterations = 1
        thoughts: list[str] = []
        intermediate_log: list[dict] = []
        termination = "max_iterations"
        conclusion = ""
        timed_out = False
        try:
            for iteration in range(1, max_iterations + 1):
                self._check()
                out = self._run_agent(
                    "controller",
                    self._think_messages(question, context, thoughts, iteration, max_iterations),
                    self._temp("controller"),
                    self._parse_thought,
                )
                thoughts.append(out["thought"])
                intermediate_log.append({"iteration": iteration, "thought": out["thought"]})
            if thoughts:
                try:
                    conclusion = self._run_agent(
                        "controller",
                        self._finalize_messages(question, thoughts),
                        self._temp("controller"),
                        self._parse_conclusion,
                    )
                except (BtcmCancelled, BtcmTimeout):
                    raise
                except BtcmError:
                    conclusion = thoughts[-1]
        except BtcmTimeout:
            termination = "timeout"
            if not thoughts:
                raise BtcmError("timeout", "整体超时且未完成任何一轮思考") from None
            conclusion = thoughts[-1]
        return {
            "conclusion": conclusion,
            "iterations_used": len(thoughts),
            "termination_reason": termination,
            "intermediate_log": intermediate_log,
        }

    # ---------- Agent 调用 ----------
    def _check(self) -> None:
        inner = self._cancel
        if inner is not None and getattr(inner, "is_cancelled", lambda: False)():
            raise BtcmCancelled("用户取消")
        if time.monotonic() >= self._deadline:
            raise BtcmTimeout("整体超时")

    def _ask(self, agent: str, messages: list[dict], temperature: float | None) -> str:
        self._check()
        buffer: list[str] = []

        def on_delta(text: str) -> None:
            buffer.append(text)
            self._emit(
                ThinkDelta(think_id=self._think_id, agent=agent, kind="content", text=text)
            )

        def on_reasoning(text: str) -> None:
            self._emit(
                ThinkDelta(think_id=self._think_id, agent=agent, kind="reasoning", text=text)
            )

        params = SessionParams(temperature=temperature) if temperature is not None else None
        usage = self._gateway.stream_chat(
            self._session_id,
            self._turn_seq,
            self._model_id,
            messages,
            self._token,
            on_delta,
            on_reasoning=on_reasoning,
            params=params,
        )
        self._add_usage(usage)
        self._check()
        return "".join(buffer)

    def _run_agent(
        self,
        agent: str,
        messages: list[dict],
        temperature: float | None,
        parse_fn: Callable[[str], Any],
        degrade: Callable[[Exception], Any] | None = None,
    ) -> Any:
        last: Exception | None = None
        for attempt in range(2):
            if attempt:
                time.sleep(RETRY_BACKOFF_SECONDS)
            try:
                return parse_fn(self._ask(agent, messages, temperature))
            except (BtcmCancelled, BtcmTimeout):
                raise
            except Exception as exc:  # noqa: BLE001 - LLM/解析失败各重试一次
                last = exc
        if degrade is not None:
            return degrade(last if last is not None else AgentOutputError("未知失败"))
        raise BtcmError("internal", f"{agent} 调用失败：{last}")

    def _validator(self, question: str, context: str | None, candidates: list[str]) -> dict:
        return self._run_agent(
            "validator",
            self._validator_messages(question, context, candidates),
            self._temp("validator"),
            self._parse_validator,
            degrade=lambda exc: _fail_report(f"验证 Agent 失败：{exc}"),
        )

    # ---------- 参数 / 计量 ----------
    def _temp(self, agent: str) -> float | None:
        value = agent_params(self._config, agent).get("temperature")
        return float(value) if value is not None else None

    def _add_usage(self, usage: Any) -> None:
        if usage is None:
            return
        for key in self._usage:
            value = getattr(usage, key, None)
            if isinstance(value, (int, float)):
                self._usage[key] += int(value)

    @staticmethod
    def _validation_conclusion(report: dict) -> str:
        verdict = report.get("verdict", "")
        best = report.get("best_candidate")
        if best:
            return f"验证结果：{verdict}；最优候选：{best}"
        return f"验证结果：{verdict}"

    # ---------- 消息构造（承原型） ----------
    def _system(self, name: str) -> str:
        return self._prompts[name] + "\n" + effort_directive(self._effort)

    def _creative_messages(
        self,
        question: str,
        context: str | None,
        current_candidate: str | None,
        validation_feedback: dict | None,
        next_direction: str | None,
        *,
        is_final: bool,
    ) -> list[dict]:
        feedback_text = "（无）"
        if validation_feedback:
            lines = []
            if validation_feedback.get("issues"):
                lines.append(
                    "上一轮验证问题：\n"
                    + "\n".join(f"- {i}" for i in validation_feedback["issues"])
                )
            if validation_feedback.get("suggestions"):
                lines.append(
                    "验证建议：\n"
                    + "\n".join(f"- {s}" for s in validation_feedback["suggestions"])
                )
            if validation_feedback.get("best_candidate"):
                lines.append(f"上一轮最优候选：{validation_feedback['best_candidate']}")
            if lines:
                feedback_text = "\n".join(lines)
        num_candidates = agent_params(self._config, "creative").get("num_candidates", 3)

        user_lines = [f"任务/用户问题：{question}"]
        if context:
            user_lines.append(f"上下文摘要：{context}")
        if current_candidate:
            user_lines.append(f"已有候选/待改进内容：{current_candidate}")
        user_lines.append(f"上一轮验证反馈：\n{feedback_text}")
        if validation_feedback:
            user_lines.append("本轮为修正轮：基于最优候选与验证问题生成 1-2 个修正候选，不重新发散。")
        else:
            user_lines.append(
                f"本轮为发散轮：请生成多个多样候选方案（配置上限 {num_candidates} 个），"
                "数量不设硬性要求，能想到几个就几个。"
            )
        if is_final:
            user_lines.append(
                "本轮是最后一轮：请直接产出可直接采纳的最终候选，确保完整、自洽、不留待后续修正。"
            )
        if next_direction:
            user_lines.append(f"总控下轮方向：{next_direction}")
        return [
            {"role": "system", "content": self._system("creative")},
            {"role": "user", "content": "\n".join(user_lines)},
        ]

    def _validator_messages(
        self, question: str, context: str | None, candidates: list[str]
    ) -> list[dict]:
        candidate_lines = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(candidates))
        user_lines = [f"任务/用户问题：{question}"]
        if context:
            user_lines.append(f"上下文摘要：{context}")
        user_lines.append(f"待验证候选：\n{candidate_lines}")
        user_lines.append("当前未启用联网搜索，请基于给定证据与逻辑进行验证。")
        return [
            {"role": "system", "content": self._system("validator")},
            {"role": "user", "content": "\n".join(user_lines)},
        ]

    def _meta_messages(
        self,
        question: str,
        context: str | None,
        iteration: int,
        candidates: list[str],
        report: dict,
        remaining: int,
    ) -> list[dict]:
        candidate_lines = "\n".join(f"- {c}" for c in candidates)
        user_lines = [f"任务/用户问题：{question}"]
        if context:
            user_lines.append(f"上下文摘要：{context}")
        user_lines.append(f"第 {iteration} 轮候选：\n{candidate_lines}")
        if remaining == 0:
            user_lines.append(
                "当前为最后一轮（无剩余轮次）：候选可用请直接 decision=stop 定稿，"
                "仅剩的轻微问题留给调用方自行消化，勿再输出 continue。"
            )
        else:
            user_lines.append(f"剩余修正轮次：{remaining}。轮次将近时请尽快考虑收束。")
        user_lines.append(
            f"验证判定：{report.get('verdict')}\n验证问题：\n"
            + "\n".join(f"- {i}" for i in report.get("issues", []))
            + "\n验证建议：\n"
            + "\n".join(f"- {s}" for s in report.get("suggestions", []))
        )
        return [
            {"role": "system", "content": self._system("meta")},
            {"role": "user", "content": "\n".join(user_lines)},
        ]

    def _think_messages(
        self,
        question: str,
        context: str | None,
        previous: list[str],
        iteration: int,
        total: int,
    ) -> list[dict]:
        user_lines = [f"任务/用户问题：{question}"]
        if context:
            user_lines.append(f"上下文摘要：{context}")
        if previous:
            user_lines.append("此前思考要点：\n" + _history_lines(previous))
        round_note = f"这是第 {iteration} 轮思考（共 {total} 轮）"
        if iteration >= total:
            round_note += "，本轮是最后一轮：请输出收敛性要点，把思考收拢到可整合为最终结论的程度"
        user_lines.append(round_note + "，请输出本轮新的思考要点。")
        return [
            {"role": "system", "content": self._system("controller")},
            {"role": "user", "content": "\n".join(user_lines)},
        ]

    def _finalize_messages(self, question: str, thoughts: list[str]) -> list[dict]:
        thought_lines = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(thoughts))
        return [
            {"role": "system", "content": self._system("controller_finalize")},
            {"role": "user", "content": f"任务/用户问题：{question}\n全部思考要点：\n{thought_lines}"},
        ]

    # ---------- 解析 ----------
    @staticmethod
    def _parse_creative(content: str) -> dict:
        obj = parse_json_object(content)
        require_keys(obj, ["candidates"], "creative")
        candidates = obj["candidates"]
        if (
            not isinstance(candidates, list)
            or not candidates
            or not all(isinstance(c, str) and c.strip() for c in candidates)
        ):
            raise AgentOutputError("Agent 'creative' 输出的 candidates 非法或为空")
        cleaned = [c.strip() for c in candidates]
        conclusion = str(obj.get("conclusion", "")).strip() or "；".join(cleaned)
        return {"candidates": cleaned, "conclusion": conclusion}

    @staticmethod
    def _parse_validator(content: str) -> dict:
        obj = parse_json_object(content)
        require_keys(obj, ["verdict"], "validator")
        verdict = obj["verdict"]
        if verdict not in VERDICTS:
            raise AgentOutputError(f"Agent 'validator' 输出非法 verdict：{verdict}")
        return {
            "verdict": verdict,
            "best_candidate": obj.get("best_candidate"),
            "issues": [str(i) for i in obj.get("issues", [])],
            "suggestions": [str(s) for s in obj.get("suggestions", [])],
            "next_actions": [str(a) for a in obj.get("next_actions", [])],
        }

    @staticmethod
    def _parse_meta(content: str) -> dict:
        obj = parse_json_object(content)
        require_keys(obj, ["conclusion"], "meta")
        decision = str(obj.get("decision", "continue")).strip().lower()
        if decision not in ("continue", "stop"):
            decision = "continue"
        return {
            "conclusion": str(obj.get("conclusion", "")),
            "remaining_issues": [str(i) for i in obj.get("remaining_issues", [])],
            "next_direction": str(obj.get("next_direction", "")).strip(),
            "decision": decision,
        }

    @staticmethod
    def _parse_thought(content: str) -> dict:
        obj = parse_json_object(content)
        require_keys(obj, ["thought"], "controller")
        thought = str(obj["thought"]).strip()
        if not thought:
            raise AgentOutputError("controller 长链思考输出为空")
        return {"thought": thought}

    @staticmethod
    def _parse_conclusion(content: str) -> str:
        obj = parse_json_object(content)
        require_keys(obj, ["conclusion"], "controller")
        return str(obj["conclusion"]).strip()