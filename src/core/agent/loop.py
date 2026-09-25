"""Agent 循环（spec §2.4 / docs 06 §3）。

首期：单次调用，无工具循环。状态机 assembling → calling → done | failed | interrupted。
- 回合开始时冻结模型绑定。
- 全部输出经 emit 回调（core → GUI 事件信封）。
- 中断：cancel_token.cancel() → AssistantFinal(interrupted=True)。
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from shared.envelope import (
    AssistantDelta,
    AssistantFinal,
    ContextUsage,
    ErrorReport,
    SendMessage,
    SessionMemoryResult,
    SessionTitleUpdated,
    TurnStatus,
    Usage,
)
from shared.errors import GATEWAY_EXCEPTION_CODE, ErrorCode, error_text
from shared.redact import redact
from shared.tokens import estimate_tokens

from core.agent.cancel import CancelToken
from core.agent.attachments import AttachmentError, load_attachments
from core.agent.context import (
    DEFAULT_SYSTEM_PROMPT,
    ConfigSnapshot,
    ContextAssembler,
)
from core.agent.memory import (
    effective_switches,
    effective_threshold,
    load_memory_prompt,
    plan_memory,
    recommended_range,
    sanitize_memory,
)
from core.agent.session import SessionStore
from core.gateway.errors import GatewayError
from core.gateway.provider import ModelGateway
from core.agent.persona import PersonaStore
from core.memory.cascade import read_cascade
from core.registry.executor import IToolExecutor, ToolContext
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

#: v0.0.11：环境声明「工具使用指引」段总预算（token，宿主常量非配置项；tool-prompts §2.2）。
_TOOL_GUIDE_TOKENS = 1200

#: rev42：ReAct 工具循环迭代上限（docs 06 §3）。达到上限后撤工具、强制模型收束作答。
MAX_TOOL_ITERATIONS = 15

#: rev59：自动标题 —— 模型提炼目标长度与首条消息截断上限（字符数）。
TITLE_MAX_CHARS = 30
TITLE_COLLAPSE = re.compile(r"\s+")

#: rev59：标题提炼提示词。客观叙述、不含身份叙事（rev16）；标题极短故不发 max_tokens（rev20）。
TITLE_PROMPT = (
    "为这段对话拟一个简短的标题。仅输出标题本身：不超过 30 个字符、单行、"
    "不要引号、不要前缀如「标题：」，可用中文。"
)


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
    def compress_memory(self, session_id: str, force: bool = False) -> None:
        """压缩较早历史为记忆文件（v0.0.1）；`force` 时强求压缩；结果经 SessionMemoryResult 回报。"""

    @abstractmethod
    def cancel(self, session_id: str) -> None:
        """对当前回合发取消令牌（协作式取消，docs 04 §2）。"""


def _code_of(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return code
    mapped = GATEWAY_EXCEPTION_CODE.get(type(exc).__name__)
    return mapped.value if mapped else ErrorCode.INTERNAL.value


def _add_usage(a: Usage, b: Usage | None) -> Usage:
    """ReAct 多迭代用量累加（rev42）。"""
    if b is None:
        return a
    return Usage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
        total_tokens=a.total_tokens + b.total_tokens,
        elapsed_ms=a.elapsed_ms + b.elapsed_ms,
        first_token_ms=a.first_token_ms or b.first_token_ms,
    )


def _tool_content(result: object) -> str:
    """工具结果 → 回注给模型的 tool 消息内容（失败时给出原因，docs 09 P5）。"""
    if getattr(result, "ok", False):
        return getattr(result, "output", None) or ""
    err = getattr(result, "error", None) or {}
    return err.get("message") or err.get("code") or "工具执行失败"


def _collapse_title(raw: str, max_chars: int = TITLE_MAX_CHARS) -> str:
    """把模型/消息文本压成**单行软截断**标题：削连续空白、换行压空格、超长软截断。

    模型侧与消息回退侧重用（spec §3 溢出边界）：`max_chars` 按字符计。
    """
    text = TITLE_COLLAPSE.sub(" ", raw or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


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
        executor: IToolExecutor | None = None,
        workspace_memory_paths: Callable[[str], list[Path]] | None = None,
        workspace_root: Callable[[str], Path | None] | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.emit = emit
        self.root = Path(root)
        self.config_store = config_store or ConfigStore(self.root)
        self.assembler = assembler or ContextAssembler()
        # rev23：角色内容解析（None 时回退默认提示词 —— 兼容既有测试的构造方式）
        self.personas = personas
        # rev42：工具执行器（None = 无工具，行为与首期一致）
        self.executor = executor
        # 工作区级联路径由 app 注入，避免 core.agent 反向依赖 core.workspace。
        self._workspace_memory_paths = workspace_memory_paths
        self._workspace_root = workspace_root
        self._active: dict[str, CancelToken] = {}
        # v0.0.1：最近一次装配用量（供阈值自动压缩判定「占用」）
        self._usage_by_session: dict[str, ContextUsage] = {}

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

    # -- 压缩（v0.0.1 · 记忆） ----------------------------------------------
    def compress_memory(self, session_id: str, force: bool = False) -> None:
        """把较早历史概括为 `memory.md`（一次独立模型调用）；失败不改动原状。

        `force=True`（用户显式点按「压缩记忆」）时**强求压缩**：忽略会话的「是否压缩」开关，
        且不按尾部保留预算，只留最后 1 轮问答、其余全部进记忆。
        """
        try:
            meta = self.store.get_meta(session_id)
        except KeyError:
            self._emit_memory_result(session_id, ok=False, error="会话不存在。")
            return
        config = self.config_store.load("memory")
        model_id = self._resolve_model(meta.main_model)
        if config.model_slot:
            model_id = self.gateway.get_slots().get(config.model_slot) or model_id
        window = meta.max_context or (self._ctx_window(model_id) if model_id else 0)
        rec_min, rec_max = recommended_range(window, config)
        _use, compress, _auto = effective_switches(meta, config)
        if not compress and not force:
            # 未强求时尊重「是否压缩」开关；强求（按钮）则继续。
            self._emit_memory_result(
                session_id,
                ok=False,
                error="本会话已关闭记忆压缩。",
                recommended_min=rec_min,
                recommended_max=rec_max,
            )
            return
        turn_seq = self.store.turn_count(session_id)
        if not model_id:
            self._emit_memory_result(
                session_id,
                ok=False,
                error="尚未绑定模型：请先在「模型配置」页为 main 槽位选择模型。",
                recommended_min=rec_min,
                recommended_max=rec_max,
            )
            return

        previous = self.store.read_memory(session_id)
        previous_text = self.store.read_memory_text(session_id) if previous else ""
        covered = previous.covered_seq if previous else -1
        keep_budget = (window // config.keep_ratio) if window else 8192
        plan = plan_memory(
            self.store.replay(session_id), previous_text, covered, keep_budget, force=force
        )
        if plan is None:
            self._emit_memory_result(
                session_id,
                ok=False,
                error=(
                    "对话还不够建立记忆：至少需要两轮问答。"
                    if force
                    else "暂无可压缩的较早内容（近段按预算保留）。"
                ),
                recommended_min=rec_min,
                recommended_max=rec_max,
            )
            return

        system_prompt = load_memory_prompt(self.root)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": plan.transcript},
        ]
        estimate = estimate_tokens(system_prompt) + estimate_tokens(plan.transcript)
        self.emit(
            TurnStatus(
                turn_seq=turn_seq,
                state="summarizing",
                note=f"正在压缩记忆：调用一次模型，预计约 {estimate} tokens。",
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
            self._fail_memory(session_id, _code_of(exc), str(exc), rec_min, rec_max)
            return
        except Exception:  # noqa: BLE001
            log.exception("压缩异常")
            self._active.pop(session_id, None)
            self._fail_memory(session_id, ErrorCode.INTERNAL.value, "内部错误", rec_min, rec_max)
            return
        self._active.pop(session_id, None)

        if token.is_cancelled():
            # spec §5：summarizing --(CancelTurn)--> interrupted（不写文件）
            self._emit_memory_result(
                session_id,
                ok=False,
                error="压缩已取消，未写入记忆。",
                recommended_min=rec_min,
                recommended_max=rec_max,
            )
            self.emit(TurnStatus(turn_seq=turn_seq, state="interrupted"))
            return

        text = sanitize_memory("".join(parts).strip())
        if not text:
            self._emit_memory_result(
                session_id,
                ok=False,
                error="记忆为空：模型未返回内容，未写入。",
                recommended_min=rec_min,
                recommended_max=rec_max,
            )
            self.emit(TurnStatus(turn_seq=turn_seq, state="done"))
            return
        memory = self.store.write_memory(
            session_id,
            covered_seq=plan.covered_seq,
            model=model_id,
            tokens_est=estimate_tokens(text),
            text=text,
        )
        self._emit_memory_result(
            session_id,
            ok=True,
            revision=memory.revision,
            covered_seq=memory.covered_seq,
            tokens_before=plan.tokens_before,
            tokens_after=memory.tokens_est,
            memory_tokens=memory.tokens_est,
            model=model_id,
            recommended_min=rec_min,
            recommended_max=rec_max,
        )
        self.emit(TurnStatus(turn_seq=turn_seq, state="done"))

    def _emit_memory_result(
        self,
        session_id: str,
        *,
        ok: bool,
        revision: int = 0,
        covered_seq: int = -1,
        tokens_before: int = 0,
        tokens_after: int = 0,
        memory_tokens: int = 0,
        recommended_min: int = 0,
        recommended_max: int = 0,
        model: str | None = None,
        error: str | None = None,
    ) -> None:
        self.emit(
            SessionMemoryResult(
                session_id=session_id,
                ok=ok,
                revision=revision,
                covered_seq=covered_seq,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                memory_tokens=memory_tokens,
                recommended_min=recommended_min,
                recommended_max=recommended_max,
                model=model,
                error=error,
            )
        )

    def _fail_memory(
        self,
        session_id: str,
        code: str,
        message: str | None,
        recommended_min: int = 0,
        recommended_max: int = 0,
    ) -> None:
        # `session.memory.result.error` 是新增回显通道 —— 上游异常文本须过统一脱敏。
        text = (redact(message) if message else None) or error_text(code)
        self._emit_memory_result(
            session_id,
            ok=False,
            error=text,
            recommended_min=recommended_min,
            recommended_max=recommended_max,
        )

    def _maybe_auto_compress(self, session_id: str, meta) -> None:
        """阈值自动压缩（v0.0.1）：仅 `use & compress & auto` 且占用 ≥ 阈值时执行。

        - `auto` 默认关；三者皆真才自动（会话覆盖优先）。
        - 「占用」取**输入侧**（装配用量去掉 reserve）占生效窗口的百分比。
        - 压缩走 `compress_memory(force=False)`，仍受「是否压缩」约束；失败/无内容由其自行回报。
        """
        config = self.config_store.load("memory")
        use, compress, auto = effective_switches(meta, config)
        if not (use and compress and auto):
            return
        usage = self._usage_by_session.get(session_id)
        if usage is None:
            return
        model_id = self._resolve_model(meta.main_model)
        window = meta.max_context or (self._ctx_window(model_id) if model_id else 0)
        if window <= 0:
            return
        input_tokens = usage.total - usage.segments.get("reserve", 0)
        if input_tokens * 100 < effective_threshold(meta, config) * window:
            return
        self.compress_memory(session_id)

    # -- 自动标题（rev59）---------------------------------------------------
    def _maybe_auto_title(self, session_id: str, meta) -> None:
        """新会话首轮后自动生成标题：模型优先 + 截断回退；失败/异常不打扰、不阻断回复。

        条件（spec §0.1 D2/D3/D4）：全局开关开 & 未手动命名 & 标题仍为默认「新对话」。
        生成成功且非空，或回退首条消息截断，均经 `store.touch_auto_title`（只改 title，
        不置 `title_manual`）后用 `SessionTitleUpdated` 广播给 GUI 刷新。
        """
        try:
            config = self.config_store.load("settings")
        except Exception:  # noqa: BLE001
            config = None
        if config is None or not getattr(config, "auto_title", True):
            return
        if getattr(meta, "title_manual", False) or (meta.title and meta.title != "新对话"):
            return
        # 首条用户消息 = 该会话第一条 `msg.user` 事件（用于模型上下文与截断回退）。
        first_text = ""
        for ev in self.store.replay(session_id):
            if ev.get("type") == "msg.user":
                first_text = (ev.get("text") or "").strip()
                break
        title = ""
        model_id = self._resolve_model(meta.main_model)
        if model_id and first_text:
            # 一次轻量非流式调用；失败/空 → 空串走截断回退（不在回复路径上，静默）。
            try:
                raw = self.gateway.generate_title(
                    [
                        {"role": "system", "content": TITLE_PROMPT},
                        {"role": "user", "content": first_text},
                    ],
                    model_id,
                )
                title = _collapse_title(raw)
            except Exception:  # noqa: BLE001 - 标题提炼失败不打扰用户
                log.debug("自动标题生成异常，回退截断首条消息", exc_info=True)
        if not title:
            title = _collapse_title(first_text)
        if not title:
            return
        try:
            self.store.touch_auto_title(session_id, title)
            self.emit(
                SessionTitleUpdated(
                    session_id=session_id,
                    workspace_id=meta.workspace_id,
                    branch_id=None,
                    title=title,
                )
            )
        except OSError:
            log.debug("自动标题落盘失败", exc_info=True)

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

        payloads = self._tool_payloads(session_id)
        messages = self._prepare_context(
            session_id, turn_seq, model_id, meta, payloads, user_message.attachments
        )
        if messages is None:
            return  # 超窗：_prepare_context 内已上报 context_overflow

        self._probe_reasoning(turn_seq, model_id)

        token = CancelToken()
        self._active[session_id] = token
        total_usage = Usage()
        final_content = ""
        final_reasoning = ""
        try:
            # rev42：ReAct 工具循环（docs 06 §3）。无工具时循环一次即结束（等价首期行为）。
            for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
                active = None if iteration >= MAX_TOOL_ITERATIONS else (payloads or None)
                self.emit(TurnStatus(turn_seq=turn_seq, state="calling"))
                parts: list[str] = []
                reasoning_parts: list[str] = []
                tool_calls: list[dict] = []
                # 回调在本轮 stream_chat 内同步消费；用默认参数显式绑定本轮缓冲，
                # 防止未来改为异步/延迟消费时闭包读到下一轮的列表（B023）。
                result = self.gateway.stream_chat(
                    session_id,
                    turn_seq,
                    model_id,
                    messages,
                    token,
                    lambda delta, buf=parts: self._on_delta(session_id, turn_seq, delta, buf),
                    on_reasoning=lambda delta, buf=reasoning_parts: self._on_reasoning(
                        session_id, turn_seq, delta, buf
                    ),
                    params=meta.params,
                    tools=active,
                    on_tool_calls=lambda calls, sink=tool_calls: sink.extend(calls),
                )
                total_usage = _add_usage(total_usage, result)
                final_content = "".join(parts)
                final_reasoning = "".join(reasoning_parts)
                if token.is_cancelled() or not tool_calls:
                    break
                self.emit(TurnStatus(turn_seq=turn_seq, state="executing"))
                self._run_tools(session_id, turn_seq, tool_calls, messages, token)
                if token.is_cancelled():
                    break
        except GatewayError as exc:
            self._active.pop(session_id, None)
            self._finish(session_id, turn_seq, final_content, interrupted=False, reasoning=final_reasoning)
            self._fail(session_id, turn_seq, _code_of(exc), str(exc))
            return
        except Exception:  # noqa: BLE001
            log.exception("回合异常")
            self._active.pop(session_id, None)
            self._finish(session_id, turn_seq, final_content, interrupted=False, reasoning=final_reasoning)
            self._fail(session_id, turn_seq, ErrorCode.INTERNAL.value, "内部错误")
            return

        self._active.pop(session_id, None)
        interrupted = token.is_cancelled()
        self._finish(
            session_id,
            turn_seq,
            final_content,
            interrupted=interrupted,
            usage=total_usage,
            reasoning=final_reasoning,
        )
        if interrupted:
            self.store.append(session_id, "user", "interrupt", {"initiator": "user"})
        self.emit(TurnStatus(turn_seq=turn_seq, state="interrupted" if interrupted else "done"))
        if not interrupted:
            # v0.0.1：回合正常结束后，按阈值自动压缩记忆（默认关；见 _maybe_auto_compress）。
            self._maybe_auto_compress(session_id, meta)
            # rev59：新会话（首轮）后自动生成标题（模型优先 + 截断回退；不在回复路径上）。
            self._maybe_auto_title(session_id, meta)

    def _tool_payloads(self, session_id: str | None = None) -> list[dict]:
        """当前可见工具的 function calling 定义（无执行器则返回空，行为同首期）。

        rev68：按会话工具白名单过滤（None 名单 = 全部可用）。
        """
        if self.executor is None:
            return []
        return self.executor.tool_payloads(session_id)

    @staticmethod
    def _tool_lines(payloads: list[dict] | None) -> list[str]:
        """把可见工具定义折成环境陈述行（`名字 — 一句话`）。

        描述取自快照（即 `ToolSpec.description`），逐条约 200 字截断以约束 token 成本；
        这是「系统提示词指导模型使用工具」的唯一改动点（spec v0.0.5 §4）——
        不新增机制、不抄 JSON Schema（FC `tools` 已下发）。
        """
        lines: list[str] = []
        for payload in payloads or []:
            fn = payload.get("function", {}) or {}
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            desc = " ".join(str(fn.get("description") or "").split())
            if len(desc) > 200:
                desc = desc[:200] + "…"
            lines.append(f"{name} — {desc}" if desc else name)
        return lines

    def _tool_guide_lines(self, session_id: str | None = None) -> tuple[list[str], int]:
        """工具使用指引段：随开关动态增删，超预算按 内置 > 技能 > MCP 截断。

        截断只影响提示词、**不影响工具可用性**，且只记一次日志（不打扰用户）。
        rev68：按会话工具白名单过滤。
        """
        if self.executor is None:
            return [], 0
        try:
            blocks = self.executor.tool_prompt_blocks(session_id)
        except Exception:  # noqa: BLE001 - 指引段非关键路径
            log.exception("读取工具使用指引失败")
            return [], 0
        lines: list[str] = []
        used = 0
        dropped = 0
        for name, block in blocks:
            text = f"{name}：{block}"
            cost = estimate_tokens(text)
            if used + cost > _TOOL_GUIDE_TOKENS:
                dropped += 1
                continue
            lines.append(text)
            used += cost
        if dropped:
            log.info("工具使用指引段超预算，已截断 %d 条（工具仍可用）", dropped)
        return lines, dropped

    def _btcm_policy_lines(self, payloads: list[dict] | None) -> list[str]:
        """自动档副思考链：环境声明挂一条**明示**策略（非隐藏注入，可审计）。

        仅当副思考链已启用、档位为自动、且 `btcm.think` 在当前可见工具中才挂载；
        默认极少触发（由模型自判「严重矛盾」，不设硬阈值）。
        """
        if not payloads or self.executor is None:
            return []
        if not any((p.get("function") or {}).get("name") == "btcm.think" for p in payloads):
            return []
        try:
            modules = self.config_store.load("modules")
        except Exception:  # noqa: BLE001 - 策略行非关键路径，读失败即不挂
            return []
        if getattr(modules.features, "btcm", False) and modules.btcm.trigger == "auto":
            return [
                "当判断问题存在严重矛盾、或需要多角度对抗检验时，可调用 btcm.think 发起一次"
                "中级思考（effort=standard）；其余情况不调用。"
            ]
        return []

    def _run_tools(
        self,
        session_id: str,
        turn_seq: int,
        tool_calls: list[dict],
        messages: list[dict],
        token: CancelToken,
    ) -> None:
        """执行一批工具调用，并把 `assistant.tool_calls` + `tool` 结果回注本次 messages。"""
        assistant_calls = []
        for call in tool_calls:
            fn = call.get("function", {}) or {}
            assistant_calls.append(
                {
                    "id": call.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    },
                }
            )
        messages.append({"role": "assistant", "content": "", "tool_calls": assistant_calls})
        # v0.0.5：把回合的取消令牌一并交给工具 —— 工具执行中也能响应中断（docs 04 §2/06 §9）。
        ctx = ToolContext(session_id=session_id, turn_seq=turn_seq, cancel=token)
        for call in tool_calls:
            if token.is_cancelled():
                break
            fn = call.get("function", {}) or {}
            call_id = call.get("id", "")
            name = fn.get("name", "")
            result = self.executor.execute_raw(call_id, name, fn.get("arguments"), ctx)
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": _tool_content(result)}
            )

    def _prepare_context(
        self, session_id: str, turn_seq: int, model_id: str, meta,
        payloads: list[dict] | None = None, attachments: list[str] | None = None,
    ) -> list[dict] | None:
        """按会话策略组装回合上下文；超窗时上报并返回 None（rev22 抽取 / rev24 改策略）。

        rev24：上下文策略随会话走 —— 生效窗口 = 会话 `max_context`，未设则用模型窗口；
        reserve/文件上限纯由窗口派生；不再有全局 `settings.context`，也不再固定轮数。
        rev42：`payloads` 非空时，把工具名写进环境陈述、工具定义 token 单列预算段。
        """
        window = meta.max_context or self._ctx_window(model_id)
        # rev23：system 首段 = 会话所用角色的 prompt.md；任何失败回退 YMT 预置（persona 侧保证）
        system_prompt = (
            self.personas.resolve_content(meta.persona_id) if self.personas else DEFAULT_SYSTEM_PROMPT
        ) or DEFAULT_SYSTEM_PROMPT
        # v0.0.1：会话记忆注入受「使用记忆」开关控制；关闭时不注入，且历史**不按覆盖点过滤**
        # （相当于完全忽略记忆；events.jsonl 只增不改，完整历史仍在）。
        use_memory, _compress, _auto = effective_switches(meta, self.config_store.load("memory"))
        memory_meta = self.store.read_memory(session_id) if use_memory else None
        memory_text = self.store.read_memory_text(session_id) if memory_meta is not None else ""
        try:
            workspace_dirs = (
                self._workspace_memory_paths(session_id)
                if self._workspace_memory_paths is not None else None
            )
        except Exception:  # noqa: BLE001 - 记忆路径缺层不得阻断基础对话
            log.exception("解析工作区记忆级联路径失败")
            workspace_dirs = []
        files: list[tuple[str, str]] = []
        if attachments:
            try:
                workspace_root = (
                    Path(self._workspace_root(session_id))
                    if self._workspace_root is not None else self.root / "workspace"
                )
                files = load_attachments(
                    workspace_root,
                    attachments,
                    self.root / "workspace" / "files",
                )
            except AttachmentError as exc:
                self._fail(
                    session_id, turn_seq, ErrorCode.INVALID_REQUEST.value,
                    redact(f"附件不可用：{exc}") or "附件不可用。",
                )
                return None
            except Exception:  # noqa: BLE001 - 读取故障不允许静默遗漏挂载
                log.exception("读取附件失败")
                self._fail(session_id, turn_seq, ErrorCode.STORAGE_ERROR.value, "读取附件失败。")
                return None
        guide_lines, _guide_dropped = self._tool_guide_lines(session_id)
        config = ConfigSnapshot(
            system_prompt=system_prompt,
            memory=read_cascade(self.root, session_id, workspace_dirs),
            session_memory=memory_text,
            files=files,
            reserve=effective_reserve(_DEFAULT_RESERVE, window),
            file_truncate=effective_file_cap(_DEFAULT_FILE, window),
            window=window,
            main_model=model_id,
            tool_lines=self._tool_lines(payloads),
            tools_tokens=(self.executor.tools_tokens(session_id) if (payloads and self.executor) else 0),
            policy_lines=self._btcm_policy_lines(payloads),
            tool_guide_lines=guide_lines,
        )
        budget = config.window - config.reserve if config.window else _UNBOUNDED
        snapshot = self.store.resume(session_id)
        if memory_meta is not None:
            snapshot.events = [
                event
                for event in snapshot.events
                if int(event.get("seq", -1)) > memory_meta.covered_seq
            ]
        messages, usage = self.assembler.build(snapshot, config, budget)
        self.store.append_event(session_id, usage)
        self.emit(usage)
        # v0.0.1：记本次装配用量，供回合结束后的阈值自动压缩判定（input 侧占用）。
        self._usage_by_session[session_id] = usage

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
