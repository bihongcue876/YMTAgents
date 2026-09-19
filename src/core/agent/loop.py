"""Agent 循环（spec §2.4 / docs 06 §3）。

首期：单次调用，无工具循环。状态机 assembling → calling → done | failed | interrupted。
- 回合开始时冻结模型绑定。
- 全部输出经 emit 回调（core → GUI 事件信封）。
- 中断：cancel_token.cancel() → AssistantFinal(interrupted=True)。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from shared.envelope import (
    AssistantDelta,
    AssistantFinal,
    ErrorReport,
    SendMessage,
    SessionSummaryResult,
    TurnStatus,
    Usage,
)
from shared.errors import GATEWAY_EXCEPTION_CODE, ErrorCode, error_text
from shared.redact import redact

from core.agent.cancel import CancelToken
from core.agent.context import (
    DEFAULT_SYSTEM_PROMPT,
    ConfigSnapshot,
    ContextAssembler,
    estimate_tokens,
)
from core.agent.summarize import (
    load_summary_prompt,
    plan_summary,
    sanitize_summary,
)
from core.agent.session import SessionStore
from core.gateway.errors import GatewayError
from core.gateway.provider import ModelGateway
from core.agent.persona import PersonaStore
from core.memory.cascade import read_cascade
from core.store.config_store import ConfigStore

log = logging.getLogger(__name__)

Emit = Callable[[object], None]
_UNBOUNDED = 10**9

#: 自适应上限（rev20，用户裁决：预算要按 200K/300K/1M 级窗口的尺度来）
_RESERVE_CAP = 32768
_FILE_CAP = 65536

#: rev24：全局上下文设置取消后，预留/文件上限只由窗口派生，这两个内部常量仅作
#: `effective_*` 的「配置下限」占位（对未知窗口仍是稳定默认），不再暴露到设置页。
_DEFAULT_RESERVE = 4096
_DEFAULT_FILE = 8192


def effective_reserve(configured: int, window: int) -> int:
    """输出预留**有效值**：随窗口自适应（≈1/8，封顶 32K），且不超过窗口 1/4。

    - 200K 窗 → 25K；300K/1M 窗 → 32K（封顶）；存量配置里的旧默认 4096 被自动放大，
      无需迁移；
    - 小窗口（8K）被 1/4 上限压回 2K —— 保证输入侧仍有一半以上窗口可用；
    - 窗口未知（ctx_window=0）用配置值。
    """
    if window <= 0:
        return configured
    target = min(window // 8, _RESERVE_CAP)
    ceiling = max(1024, window // 4)
    return min(max(configured, target), ceiling)


def effective_file_cap(configured: int, window: int) -> int:
    """挂载文件**每文件**上限有效值：≈窗口 1/4，封顶 64K；窗口未知用配置值。"""
    if window <= 0:
        return configured
    return max(configured, min(window // 4, _FILE_CAP))


def model_ctx_window(gateway, model_id: str) -> int:
    """模型声明的上下文窗口（token）；未知返回 0（rev24：供 loop 与 controller 共用）。"""
    for provider in gateway.list_providers():
        for model in provider.models:
            if model.id == model_id:
                return model.ctx_window
    return 0


class IAgentLoop(ABC):
    @abstractmethod
    def run_turn(self, session_id: str, user_message: SendMessage) -> None: ...

    @abstractmethod
    def summarize(self, session_id: str) -> None:
        """压缩较早历史为摘要文件（rev26）；结果经 SessionSummaryResult 事件回报。"""

    @abstractmethod
    def cancel(self, session_id: str) -> None:
        """对当前回合发取消令牌（协作式取消，docs 04 §2）。"""


def _code_of(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return code
    mapped = GATEWAY_EXCEPTION_CODE.get(type(exc).__name__)
    return mapped.value if mapped else ErrorCode.INTERNAL.value


class AgentLoop(IAgentLoop):
    def __init__(
        self,
        store: SessionStore,
        gateway: ModelGateway,
        emit: Emit,
        root: Path,
        config_store: ConfigStore | None = None,
        assembler: ContextAssembler | None = None,
        personas: "PersonaStore | None" = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.emit = emit
        self.root = Path(root)
        self.config_store = config_store or ConfigStore(self.root)
        self.assembler = assembler or ContextAssembler()
        # rev23：角色内容解析（None 时回退默认提示词 —— 兼容既有测试的构造方式）
        self.personas = personas
        self._active: dict[str, CancelToken] = {}

    def cancel(self, session_id: str) -> None:
        token = self._active.get(session_id)
        if token is not None:
            token.cancel()

    # -- 内部 --------------------------------------------------------------
    def _resolve_model(self, main_model: str | None) -> str | None:
        return main_model or self.gateway.get_slots().get("main")

    def _ctx_window(self, model_id: str) -> int:
        return model_ctx_window(self.gateway, model_id)

    def _fail(self, session_id: str, turn_seq: int, code: str, message: str | None = None) -> None:
        """失败收口。message 缺省时取码对应的中文提示（shared.errors.ERROR_TEXT）。"""
        text = message or error_text(code)
        err = ErrorReport(scope="session", code=code, message=text)
        self.store.append_event(session_id, err)
        self.emit(err)
        self.emit(TurnStatus(turn_seq=turn_seq, state="failed", error=text))

    def _on_delta(self, session_id: str, turn_seq: int, delta: str, parts: list[str]) -> None:
        parts.append(delta)
        event = AssistantDelta(content=delta, turn_seq=turn_seq)
        self.store.append_event(session_id, event)
        self.emit(event)

    def _on_reasoning(self, session_id: str, turn_seq: int, delta: str, parts: list[str]) -> None:
        """思考增量（rev25）：与正文分流上报，GUI 收进折叠块。"""
        parts.append(delta)
        event = AssistantDelta(content=delta, turn_seq=turn_seq, reasoning=True)
        self.store.append_event(session_id, event)
        self.emit(event)

    def _probe_reasoning(self, turn_seq: int, model_id: str) -> None:
        """调用前一次性探测思考能力（rev25）。

        先发成本预告（用户裁决：需预告、单次 50–100 token、不每次都测）；
        探测失败不阻断本回合 —— 能力留在 unknown，由用户可在模型页人工覆盖。
        """
        if not self.gateway.reasoning_pending(model_id):
            return
        self.emit(
            TurnStatus(
                turn_seq=turn_seq,
                state="probing",
                note="首次使用该模型：正在探测思考能力（约消耗 50–100 tokens，仅一次）",
            )
        )
        self.gateway.probe_reasoning(model_id)

    # -- 压缩（rev26 · 轮 C） ----------------------------------------------
    def summarize(self, session_id: str) -> None:
        """把较早历史概括为 `summary.md`（一次独立模型调用）；失败不改动原状。"""
        try:
            meta = self.store.get_meta(session_id)
        except KeyError:
            self._emit_summary_result(session_id, ok=False, error="会话不存在。")
            return
        config = self.config_store.load("summary")
        model_id = self._resolve_model(meta.main_model)
        if config.model_slot:
            model_id = self.gateway.get_slots().get(config.model_slot) or model_id
        turn_seq = self.store.turn_count(session_id)
        if not model_id:
            self._emit_summary_result(
                session_id,
                ok=False,
                error="尚未绑定模型：请先在「模型配置」页为 main 槽位选择模型。",
            )
            return

        previous = self.store.read_summary(session_id)
        previous_text = self.store.read_summary_text(session_id) if previous else ""
        covered = previous.covered_seq if previous else -1
        window = meta.max_context or self._ctx_window(model_id)
        keep_budget = (window // config.keep_ratio) if window else 8192
        plan = plan_summary(self.store.replay(session_id), previous_text, covered, keep_budget)
        if plan is None:
            self._emit_summary_result(
                session_id, ok=False, error="暂无可压缩的较早内容（近段按预算保留）。"
            )
            return

        system_prompt = load_summary_prompt(self.root)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": plan.transcript},
        ]
        estimate = estimate_tokens(system_prompt) + estimate_tokens(plan.transcript)
        self.emit(
            TurnStatus(
                turn_seq=turn_seq,
                state="summarizing",
                note=f"正在压缩历史：调用一次摘要模型，预计约 {estimate} tokens。",
            )
        )
        token = CancelToken()
        self._active[session_id] = token
        parts: list[str] = []
        try:
            self.gateway.stream_chat(
                session_id,
                turn_seq,
                model_id,
                messages,
                token,
                lambda delta: parts.append(delta),
            )
        except GatewayError as exc:
            self._active.pop(session_id, None)
            self._fail_summary(session_id, _code_of(exc), str(exc))
            return
        except Exception:  # noqa: BLE001
            log.exception("压缩异常")
            self._active.pop(session_id, None)
            self._fail_summary(session_id, ErrorCode.INTERNAL.value, "内部错误")
            return
        self._active.pop(session_id, None)

        text = sanitize_summary("".join(parts).strip())
        if not text:
            self._emit_summary_result(
                session_id, ok=False, error="摘要为空：模型未返回内容，未写入。"
            )
            self.emit(TurnStatus(turn_seq=turn_seq, state="done"))
            return
        summary = self.store.write_summary(
            session_id,
            covered_seq=plan.covered_seq,
            model=model_id,
            tokens_est=estimate_tokens(text),
            text=text,
        )
        self._emit_summary_result(
            session_id,
            ok=True,
            revision=summary.revision,
            covered_seq=summary.covered_seq,
            tokens_before=plan.tokens_before,
            tokens_after=summary.tokens_est,
            summary_tokens=summary.tokens_est,
            model=model_id,
        )
        self.emit(TurnStatus(turn_seq=turn_seq, state="done"))

    def _emit_summary_result(
        self,
        session_id: str,
        *,
        ok: bool,
        revision: int = 0,
        covered_seq: int = -1,
        tokens_before: int = 0,
        tokens_after: int = 0,
        summary_tokens: int = 0,
        model: str | None = None,
        error: str | None = None,
    ) -> None:
        self.emit(
            SessionSummaryResult(
                session_id=session_id,
                ok=ok,
                revision=revision,
                covered_seq=covered_seq,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                summary_tokens=summary_tokens,
                model=model,
                error=error,
            )
        )

    def _fail_summary(self, session_id: str, code: str, message: str | None) -> None:
        # rev27：`session.summary.result.error` 是新增回显通道 —— 上游异常文本须过统一脱敏。
        text = (redact(message) if message else None) or error_text(code)
        self._emit_summary_result(session_id, ok=False, error=text)

    # -- 回合 --------------------------------------------------------------
    def run_turn(self, session_id: str, user_message: SendMessage) -> None:
        meta = self.store.get_meta(session_id)
        turn_seq = self.store.turn_count(session_id)
        self.store.append_event(session_id, user_message)
        self.emit(TurnStatus(turn_seq=turn_seq, state="assembling"))

        model_id = self._resolve_model(meta.main_model)
        if not model_id:
            # 「未绑定」是**无引用**，不是「找不到」——不得报 provider_not_found（spec rev5 §2）
            self._fail(
                session_id,
                turn_seq,
                ErrorCode.MODEL_UNBOUND.value,
                "尚未绑定模型：请在「模型配置」页为 main 槽位选择一个模型，"
                "或在对话顶部的模型下拉中选择。",
            )
            return

        messages = self._prepare_context(session_id, turn_seq, model_id, meta)
        if messages is None:
            return  # 超窗：_prepare_context 内已上报 context_overflow

        self._probe_reasoning(turn_seq, model_id)

        self.emit(TurnStatus(turn_seq=turn_seq, state="calling"))
        token = CancelToken()
        self._active[session_id] = token
        parts: list[str] = []
        reasoning_parts: list[str] = []
        try:
            result = self.gateway.stream_chat(
                session_id,
                turn_seq,
                model_id,
                messages,
                token,
                lambda delta: self._on_delta(session_id, turn_seq, delta, parts),
                on_reasoning=lambda delta: self._on_reasoning(
                    session_id, turn_seq, delta, reasoning_parts
                ),
                params=meta.params,
            )
        except GatewayError as exc:
            self._active.pop(session_id, None)
            self._finish(
                session_id,
                turn_seq,
                "".join(parts),
                interrupted=False,
                reasoning="".join(reasoning_parts),
            )
            self._fail(session_id, turn_seq, _code_of(exc), str(exc))
            return
        except Exception:  # noqa: BLE001
            log.exception("回合异常")
            self._active.pop(session_id, None)
            self._finish(
                session_id,
                turn_seq,
                "".join(parts),
                interrupted=False,
                reasoning="".join(reasoning_parts),
            )
            self._fail(session_id, turn_seq, ErrorCode.INTERNAL.value, "内部错误")
            return

        self._active.pop(session_id, None)
        interrupted = token.is_cancelled()
        self._finish(
            session_id,
            turn_seq,
            "".join(parts),
            interrupted=interrupted,
            usage=result,
            reasoning="".join(reasoning_parts),
        )
        if interrupted:
            self.store.append(session_id, "user", "interrupt", {"initiator": "user"})
        self.emit(TurnStatus(turn_seq=turn_seq, state="interrupted" if interrupted else "done"))

    def _prepare_context(
        self, session_id: str, turn_seq: int, model_id: str, meta
    ) -> list[dict] | None:
        """按会话策略组装回合上下文；超窗时上报并返回 None（rev22 抽取 / rev24 改策略）。

        rev24：上下文策略随会话走 —— 生效窗口 = 会话 `max_context`，未设则用模型窗口；
        reserve/文件上限纯由窗口派生；不再有全局 `settings.context`，也不再固定轮数。
        """
        window = meta.max_context or self._ctx_window(model_id)
        # rev23：system 首段 = 会话所用角色的 prompt.md；任何失败回退 YMT 预置（persona 侧保证）
        system_prompt = (
            self.personas.resolve_content(meta.persona_id) if self.personas else DEFAULT_SYSTEM_PROMPT
        ) or DEFAULT_SYSTEM_PROMPT
        # rev26：有摘要时，摘要替代其覆盖点之前的历史；近段历史仍按 token 预算参与装配。
        summary_meta = self.store.read_summary(session_id)
        summary_text = self.store.read_summary_text(session_id) if summary_meta else ""
        config = ConfigSnapshot(
            system_prompt=system_prompt,
            memory=read_cascade(self.root, session_id),
            summary=summary_text,
            reserve=effective_reserve(_DEFAULT_RESERVE, window),
            file_truncate=effective_file_cap(_DEFAULT_FILE, window),
            window=window,
            main_model=model_id,
            tool_names=[],
        )
        budget = config.window - config.reserve if config.window else _UNBOUNDED
        snapshot = self.store.resume(session_id)
        if summary_meta is not None:
            snapshot.events = [
                event
                for event in snapshot.events
                if int(event.get("seq", -1)) > summary_meta.covered_seq
            ]
        messages, usage = self.assembler.build(snapshot, config, budget)
        self.store.append_event(session_id, usage)
        self.emit(usage)

        # 超窗本地拦截（spec §7 / rev8 §2）：淘汰已保不住当前提问时，请求必然被上游拒绝，
        # 且往往被上游报成 model_not_found 一类误导性错误。判据取**输入侧**用量
        # （total 去掉 reserve）—— 窗口约束的是送进去的上下文；reserve 只是输出预留。
        input_tokens = usage.total - usage.segments.get("reserve", 0)
        if config.window and input_tokens > config.window:
            self._fail(
                session_id,
                turn_seq,
                ErrorCode.CONTEXT_OVERFLOW.value,
                f"上下文已超出本会话窗口（估算 {input_tokens} tokens > {config.window}）："
                "可在右侧「会话详情」中调大上下文上限，或开始新对话。",
            )
            return None
        return messages

    def _finish(
        self,
        session_id: str,
        turn_seq: int,
        content: str,
        interrupted: bool,
        usage=None,
        reasoning: str = "",
    ) -> None:
        final = AssistantFinal(
            content=content,
            turn_seq=turn_seq,
            usage=usage if usage is not None else Usage(),
            interrupted=interrupted,
            reasoning=reasoning,
        )
        self.store.append_event(session_id, final)
        self.store.touch(session_id)
        self.emit(final)
