"""工具执行管线与关卡（rev43 / docs 07 §3 / docs 09）。

七步管线：快照校验 → 可见性 → 权限判定 → tool.call 落盘 → 执行 → tool.result 落盘 → 计量回填。

安全铁律（docs 09）：
- 有效权限 = 逐工具显式覆盖 ⊕ 缺省（无通配、运行期不可提权）；覆盖在注册时已落进 `ToolSpec.permission`。
- 调用前策略检查（`ToolSpec.precheck`，v0.0.5）：按**参数**判定的高危档在关卡**之前**收口 ——
  高危命令不该先打扰用户再被后端拒绝；命中 `deny` 即策略拒绝并留痕，命中 `warn` 则关卡卡片按高危档展示。
- confirm 工具逐次关卡：登记 `GateRequest`，由用户裁决；超时/中断在途确认一律**拒绝**（fail-closed）。
- restricted 工具默认关：策略直接拒绝并留痕。
- 拒绝返回**理由字符串**（回给模型，docs 09 §5）；`tool.call` / `tool.result` / `gate.result`
  均为**一等落盘事件**（审计 append-only）。
- 出口文本（错误 detail）一律过 `redact`。
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from shared.enums import Permission
from shared.envelope import GateRequest, GateResult, ToolCall, ToolResult as ToolResultEvent
from shared.errors import ErrorCode, error_text
from shared.redact import redact
from shared.tokens import estimate_tokens

from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec

log = logging.getLogger(__name__)

#: confirm 关卡等待上限（docs 09 §3：超时视为拒绝）。
GATE_TIMEOUT_S = 300.0

#: 工具输出内联上限（字符）：超过则外置到会话目录，事件流只留预览（output_ref）。
INLINE_OUTPUT_CHARS = 8000

#: 关卡裁决回调：入参 (call_id, name, args, permission)，返回是否放行（False = 拒绝/超时）。
GateHandler = Callable[[str, str, dict, str], bool]


@dataclass
class ToolContext:
    """执行上下文（会话/回合标识），用于把 tool.* 事件落进正确会话。

    `cancel`（v0.0.5）：回合的协作式取消令牌（docs 04 §2「工具执行中 → 工具取消路径」）。
    用具名弱接口传（只要求有 `is_cancelled()`），避免 `core.registry` 反向依赖 `core.agent`。
    """

    session_id: str
    turn_seq: int = 0
    cancel: object | None = None


class IToolExecutor(ABC):
    @abstractmethod
    def set_gate(self, gate: GateHandler | None) -> None:
        """注入关卡裁决回调（app 层装配后回填）。"""

    @abstractmethod
    def tool_payloads(self) -> list[dict]:
        """当前可见工具的 OpenAI function calling 定义列表。"""

    @abstractmethod
    def tools_tokens(self) -> int:
        """工具定义的输入侧 token 估算（单列预算段）。"""

    @abstractmethod
    def execute(self, call_id: str, name: str, args: dict, ctx: ToolContext | None = None) -> ToolResult:
        """执行一次工具调用（含权限关卡与落盘）。"""

    @abstractmethod
    def execute_raw(self, call_id: str, name: str, raw_arguments: object,
                    ctx: ToolContext | None = None) -> ToolResult:
        """从模型返回的**原始** arguments（JSON 串或 dict）解析后执行；解析失败归 invalid_args。"""


def _perm_value(perm: Any) -> str:
    return perm.value if isinstance(perm, Permission) else str(perm or Permission.CONFIRM.value)


class ToolExecutor(IToolExecutor):
    def __init__(
        self,
        registry: Registry,
        store: Any = None,
        emit: Callable[[object], None] | None = None,
        audit: Callable[..., None] | None = None,
        gate: GateHandler | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.emit = emit or (lambda _event: None)
        self.audit = audit or (lambda *_a, **_k: None)
        self._gate = gate

    def set_gate(self, gate: GateHandler | None) -> None:
        """注入关卡裁决回调（由 app 层在装配后回填，core 不依赖 app）。"""
        self._gate = gate

    # -- 可见性与定义 ------------------------------------------------------
    def _is_visible(self, spec: ToolSpec) -> bool:
        avail = spec.availability
        if avail is None:
            return True
        try:
            return bool(avail() if callable(avail) else avail)
        except Exception:  # noqa: BLE001 - 可用性回调异常时 fail-closed（不可见）
            return False

    def visible_specs(self) -> list[ToolSpec]:
        return self.registry.list_tools(self._is_visible)

    def tool_payloads(self) -> list[dict]:
        payloads: list[dict] = []
        for spec in self.visible_specs():
            payloads.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description or spec.title or spec.name,
                        "parameters": spec.input_schema
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        return payloads

    def tools_tokens(self) -> int:
        # 粗略估算：工具名 + 描述 + 入参 schema 的字符量。
        total = 0
        for payload in self.tool_payloads():
            total += estimate_tokens(json.dumps(payload, ensure_ascii=False))
        return total

    # -- 执行 --------------------------------------------------------------
    def _find_spec(self, name: str) -> ToolSpec | None:
        for spec in self.registry.snapshot():
            if spec.name == name:
                return spec
        return None

    def execute_raw(self, call_id: str, name: str, raw_arguments: object,
                    ctx: ToolContext | None = None) -> ToolResult:
        """解析模型返回的原始 arguments（JSON 串/dict）；解析失败 → invalid_args（不执行）。"""
        args: dict = {}
        if isinstance(raw_arguments, dict):
            args = raw_arguments
        elif raw_arguments:
            try:
                parsed = json.loads(raw_arguments)
            except (ValueError, TypeError):
                result = self._error(ErrorCode.TOOL_INVALID_ARGS)
                self._persist(call_id, name, {}, Permission.CONFIRM.value, result, ctx)
                return result
            if isinstance(parsed, dict):
                args = parsed
        return self.execute(call_id, name, args, ctx)

    def execute(self, call_id: str, name: str, args: dict, ctx: ToolContext | None = None) -> ToolResult:
        spec = self._find_spec(name)
        if spec is None or not self._is_visible(spec):
            result = self._error(ErrorCode.TOOL_UNAVAILABLE)
            self._persist(call_id, name, args, Permission.CONFIRM.value, result, ctx)
            return result

        permission = _perm_value(spec.permission)
        self._persist_call(call_id, name, args, permission, ctx)

        denial = self._authorize(call_id, name, args, permission, ctx, spec)
        if denial is not None:
            result = self._error(ErrorCode.TOOL_DENIED, denial)
            self._persist_result(call_id, result, ctx)
            self.audit("tool.invoke", tool=name, permission=permission, ok=False, code=ErrorCode.TOOL_DENIED.value)
            return result

        started = time.monotonic()
        try:
            result = self.registry.execute(name, args, ctx)
        except Exception as exc:  # noqa: BLE001 - 后端异常归 backend_error，不留栈于出口
            log.exception("工具执行异常：%s", name)
            result = ToolResult(
                ok=False,
                error={
                    "code": ErrorCode.TOOL_BACKEND_ERROR.value,
                    "message": redact(str(exc)) or error_text(ErrorCode.TOOL_BACKEND_ERROR.value),
                },
            )
        if not result.duration_ms:
            result = result.model_copy(update={"duration_ms": int((time.monotonic() - started) * 1000)})
        result = self._externalize(name, call_id, result, ctx)

        self._persist_result(call_id, result, ctx)
        self.audit(
            "tool.invoke",
            tool=name,
            permission=permission,
            ok=result.ok,
            code=(result.error or {}).get("code"),
        )
        return result

    def _authorize(self, call_id: str, name: str, args: dict, permission: str,
                   ctx: ToolContext | None, spec: ToolSpec) -> str | None:
        """权限判定。返回 `None` = 放行；否则返回**拒绝理由**（回给模型，docs 09 §5）。

        顺序（docs 07 §3 步骤 3）：
        0. 调用前策略检查（`ToolSpec.precheck`）：`deny` → 策略直接拒绝（**不打扰用户**），
           `warn` → 继续走关卡，但卡片按高危档展示；
        1. safe 直放；2. restricted 策略拒绝；3. confirm 走用户关卡。
        检查器自身异常一律按拒绝处理（P6 安全默认）。
        """
        warn = ""
        precheck = getattr(spec, "precheck", None)
        if callable(precheck):
            try:
                verdict = precheck(args or {})
            except Exception:  # noqa: BLE001 - 策略检查异常必须 fail-closed
                log.exception("工具调用前策略检查异常：%s", name)
                self._persist_gate(call_id, "deny", "policy", ctx)
                self.audit("gate.decision", tool=name, decision="deny", decider="policy")
                return error_text(ErrorCode.TOOL_DENIED.value)
            if verdict:
                kind, reason = verdict[0], str(verdict[1] or "")
                if kind == "deny":
                    self._persist_gate(call_id, "deny", "policy", ctx)
                    self.audit("gate.decision", tool=name, decision="deny", decider="policy")
                    return reason or error_text(ErrorCode.TOOL_DENIED.value)
                warn = reason

        if permission == Permission.SAFE.value:
            return None
        if permission == Permission.RESTRICTED.value:
            self._persist_gate(call_id, "deny", "policy", ctx)
            self.audit("gate.decision", tool=name, decision="deny", decider="policy")
            return error_text(ErrorCode.TOOL_DENIED.value)
        # confirm：向 UI 请求确认（含参数原文），等待用户裁决；无裁决通道时 fail-closed。
        # `warn` 非空 = 按参数判定的高危：卡片以 restricted 档呈现，用户一眼可见。
        label = Permission.RESTRICTED.value if warn else permission
        self.emit(GateRequest(call_id=call_id, name=name, args=args, permission=label))
        allow = bool(self._gate(call_id, name, args, permission)) if self._gate is not None else False
        self._persist_gate(call_id, "allow" if allow else "deny", "user", ctx)
        self.audit("gate.decision", tool=name, decision="allow" if allow else "deny", decider="user")
        return None if allow else error_text(ErrorCode.TOOL_DENIED.value)

    # -- 落盘 --------------------------------------------------------------
    def _externalize(self, name: str, call_id: str, result: ToolResult,
                     ctx: ToolContext | None) -> ToolResult:
        """超大输出外置：全文写入会话 outputs/<call_id>.txt，事件流只留预览 + output_ref。

        `skill.*` 豁免（v0.0.4）：技能正文是渐进披露的**一等输入**而非工具输出，
        外置成预览会让模型读不到指令——其体量上限在加载期收口（20K 字符 fail-closed）。

        外置失败不阻断工具结果（仅记日志）；无 store/无会话上下文时保持内联。
        """
        output = result.output
        if not output or len(output) <= INLINE_OUTPUT_CHARS or ctx is None:
            return result
        if name.startswith("skill."):
            return result
        writer = getattr(self.store, "write_output", None)
        if writer is None:
            return result
        try:
            ref = writer(ctx.session_id, call_id, output)
        except Exception:  # noqa: BLE001 - 外置非关键路径，失败即回退内联
            log.exception("工具输出外置失败：%s", call_id)
            return result
        if not ref:
            return result
        preview = output[:INLINE_OUTPUT_CHARS] + f"\n…（输出已外置，完整内容见 {ref}）"
        return result.model_copy(update={"output": preview, "output_ref": ref})

    def _error(self, code: ErrorCode, message: str | None = None) -> ToolResult:
        return ToolResult(
            ok=False, error={"code": code.value, "message": message or error_text(code.value)}
        )

    def _persist(self, call_id: str, name: str, args: dict, permission: str, result: ToolResult,
                 ctx: ToolContext | None) -> None:
        self._persist_call(call_id, name, args, permission, ctx)
        self._persist_result(call_id, result, ctx)

    def _persist_call(self, call_id: str, name: str, args: dict, permission: str,
                      ctx: ToolContext | None) -> None:
        event = ToolCall(call_id=call_id, name=name, args=args, permission=permission)
        if ctx is not None and self.store is not None:
            self.store.append_event(ctx.session_id, event)
        self.emit(event)

    def _persist_result(self, call_id: str, result: ToolResult, ctx: ToolContext | None) -> None:
        event = ToolResultEvent(
            call_id=call_id,
            ok=result.ok,
            output=result.output,
            output_ref=result.output_ref,
            usage=result.usage,
            duration_ms=result.duration_ms,
            error=result.error,
        )
        if ctx is not None and self.store is not None:
            self.store.append_event(ctx.session_id, event)
        self.emit(event)

    def _persist_gate(self, call_id: str, decision: str, decider: str,
                      ctx: ToolContext | None) -> None:
        # gate.result 落 events.jsonl（审计 append-only）；历史重建用 tool.call/result，不用本事件。
        event = GateResult(call_id=call_id, decision=decision, decider=decider)
        if ctx is not None and self.store is not None:
            self.store.append_event(ctx.session_id, event)
        self.emit(event)