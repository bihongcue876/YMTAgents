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
    TurnStatus,
    Usage,
)
from shared.errors import GATEWAY_EXCEPTION_CODE, ErrorCode, error_text

from core.agent.cancel import CancelToken
from core.agent.context import DEFAULT_SYSTEM_PROMPT, ConfigSnapshot, ContextAssembler
from core.agent.session import SessionStore
from core.gateway.errors import GatewayError
from core.gateway.provider import ModelGateway
from core.memory.cascade import read_cascade
from core.store.config_store import ConfigStore

log = logging.getLogger(__name__)

Emit = Callable[[object], None]
_UNBOUNDED = 10**9


class IAgentLoop(ABC):
    @abstractmethod
    def run_turn(self, session_id: str, user_message: SendMessage) -> None: ...

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
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.emit = emit
        self.root = Path(root)
        self.config_store = config_store or ConfigStore(self.root)
        self.assembler = assembler or ContextAssembler()
        self._active: dict[str, CancelToken] = {}

    def cancel(self, session_id: str) -> None:
        token = self._active.get(session_id)
        if token is not None:
            token.cancel()

    # -- 内部 --------------------------------------------------------------
    def _resolve_model(self, main_model: str | None) -> str | None:
        return main_model or self.gateway.get_slots().get("main")

    def _ctx_window(self, model_id: str) -> int:
        for provider in self.gateway.list_providers():
            for model in provider.models:
                if model.id == model_id:
                    return model.ctx_window
        return 0

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

        settings = self.config_store.load("settings")
        config = ConfigSnapshot(
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            memory=read_cascade(self.root, session_id),
            history_turns=settings.context.history_turns,
            reserve=settings.context.reserve,
            file_truncate=settings.context.file_truncate,
            window=self._ctx_window(model_id),
            main_model=model_id,
            tool_names=[],
        )
        budget = config.window - config.reserve if config.window else _UNBOUNDED
        snapshot = self.store.resume(session_id)
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
                f"上下文已超出模型窗口（估算 {input_tokens} tokens > {config.window}）："
                "请在「系统设置 → 上下文策略」下调历史保留轮数或挂载文件截断上限。",
            )
            return

        self.emit(TurnStatus(turn_seq=turn_seq, state="calling"))
        token = CancelToken()
        self._active[session_id] = token
        parts: list[str] = []
        try:
            result = self.gateway.stream_chat(
                session_id,
                turn_seq,
                model_id,
                messages,
                token,
                lambda delta: self._on_delta(session_id, turn_seq, delta, parts),
            )
        except GatewayError as exc:
            self._active.pop(session_id, None)
            self._finish(session_id, turn_seq, "".join(parts), interrupted=False)
            self._fail(session_id, turn_seq, _code_of(exc), str(exc))
            return
        except Exception:  # noqa: BLE001
            log.exception("回合异常")
            self._active.pop(session_id, None)
            self._finish(session_id, turn_seq, "".join(parts), interrupted=False)
            self._fail(session_id, turn_seq, ErrorCode.INTERNAL.value, "内部错误")
            return

        self._active.pop(session_id, None)
        interrupted = token.is_cancelled()
        self._finish(session_id, turn_seq, "".join(parts), interrupted=interrupted, usage=result)
        if interrupted:
            self.store.append(session_id, "user", "interrupt", {"initiator": "user"})
        self.emit(TurnStatus(turn_seq=turn_seq, state="interrupted" if interrupted else "done"))

    def _finish(
        self,
        session_id: str,
        turn_seq: int,
        content: str,
        interrupted: bool,
        usage=None,
    ) -> None:
        final = AssistantFinal(
            content=content,
            turn_seq=turn_seq,
            usage=usage if usage is not None else Usage(),
            interrupted=interrupted,
        )
        self.store.append_event(session_id, final)
        self.store.touch(session_id)
        self.emit(final)
