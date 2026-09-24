"""BTCM 宿主（切片 2）：作为附加功能接入 `FeatureManager`。

- `activate`：读配置 + 注册**只读**工具 `btcm.think`（`permission=safe`）。
- `deactivate`：注销工具（真卸载）；引擎随宿主丢弃。
- 模型路由（D4）：本对话模型优先 → 页面指定槽位 → 全局默认，均经宿主网关。
- 独立计量：引擎累计 `Usage` → `ToolResult.usage`（经执行器第七步落事件流）。
- 子选项 `btcm.update`（手动/自动、槽位）；态经 `btcm.state` 呈现。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from shared.enums import Permission
from shared.errors import ErrorCode, error_text
from shared.redact import redact
from shared.schema import BtcmConfig

from core.modules.btcm.config import resolve_model
from core.modules.btcm.engine import BtcmCancelled, BtcmEngine, BtcmError, BtcmTimeout
from core.modules.feature import IFeatureHost
from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec

TOOL_THINK = "btcm.think"
TOOL_TIMEOUT_MS = 300_000
_SLOTS = ("main", "thinking", "fast", "embedding")

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "需要深入思考的问题或任务"},
        "effort": {
            "type": "string",
            "enum": ["light", "standard", "deep"],
            "description": "思考深度：略想 / 通用 / 深层（默认 standard）",
        },
        "candidate": {"type": "string", "description": "待改进或待验证的候选内容（可选）"},
        "context": {"type": "string", "description": "上下文摘要（可选）"},
        "mode": {
            "type": "string",
            "enum": ["auto", "creative", "validate", "long"],
            "description": "运行形态（默认 auto，按配置）",
        },
    },
    "required": ["question"],
}


class BtcmManager(IFeatureHost):
    def __init__(
        self,
        registry: Registry,
        config_store: Any,
        gateway: Any,
        *,
        emit: Callable[[Any], None] | None = None,
        audit: Callable[..., None] | None = None,
        resolve_session_model: Callable[[str], str | None] | None = None,
    ) -> None:
        self._registry = registry
        self._config_store = config_store
        self._gateway = gateway
        self._emit = emit or (lambda _e: None)
        self._audit = audit or (lambda *_a, **_k: None)
        self._resolve_session_model = resolve_session_model or (lambda _sid: None)
        self._config = BtcmConfig()
        self._registered = False

    # ---------- 附加功能契约 ----------
    def activate(self) -> None:
        self._load()
        self._register()

    def deactivate(self) -> None:
        self._unregister()

    def host_state(self) -> str:
        return "ready" if self._registered else "disabled"

    # ---------- 配置 ----------
    def _load(self) -> None:
        try:
            self._config = self._config_store.load("modules").btcm
        except Exception:  # noqa: BLE001 - 配置异常回退默认，不阻断装配
            self._config = BtcmConfig()

    def _register(self) -> None:
        if self._registered:
            self._unregister()
        spec = ToolSpec(
            name=TOOL_THINK,
            title="副思考链",
            description="对疑难问题做多候选生成与对抗验证，返回结论与依据摘要。",
            permission=Permission.SAFE,
            input_schema=_INPUT_SCHEMA,
            timeout_ms=TOOL_TIMEOUT_MS,
        )
        self._registry.register(spec, self._handle)
        self._registered = True

    def _unregister(self) -> None:
        if self._registered:
            self._registry.unregister(TOOL_THINK)
            self._registered = False

    def update(self, trigger: str | None = None, slot: str | None = None) -> None:
        modules = self._config_store.load("modules")
        if trigger in ("manual", "auto"):
            modules.btcm.trigger = trigger
        if slot in _SLOTS:
            modules.btcm.slot = slot
        self._config_store.save("modules", modules)
        self._config = modules.btcm
        self._audit("btcm.update", trigger=modules.btcm.trigger, slot=modules.btcm.slot)

    def state_payload(self) -> dict:
        # 存量 `off` 归一为 `manual`（宿主级启停由 features.btcm 决定）。
        trigger = self._config.trigger if self._config.trigger in ("manual", "auto") else "manual"
        return {
            "trigger": trigger,
            "slot": self._config.slot,
            "ready": self._registered,
        }

    # ---------- 工具执行 ----------
    def _handle(self, args: dict, ctx: Any = None) -> ToolResult:
        question = str(args.get("question") or "").strip()
        if not question:
            return self._error(ErrorCode.TOOL_INVALID_ARGS, "question 不能为空。")
        effort = str(args.get("effort") or "standard")
        if effort not in ("light", "standard", "deep"):
            effort = "standard"
        mode = str(args.get("mode") or "auto")
        if mode not in ("auto", "creative", "validate", "long"):
            mode = "auto"
        candidate = args.get("candidate") or None
        context = args.get("context") or None
        session_id = str(getattr(ctx, "session_id", "") or "")
        turn_seq = int(getattr(ctx, "turn_seq", 0) or 0)
        cancel = getattr(ctx, "cancel", None)
        think_id = str(getattr(ctx, "call_id", "") or "") or session_id or TOOL_THINK

        model_id = self._resolve_model(session_id)
        if not model_id:
            return self._error(
                ErrorCode.TOOL_UNAVAILABLE,
                "副思考链未绑定模型：请在模型页设置全局默认，或在会话中选择模型。",
            )
        try:
            engine = BtcmEngine(
                self._gateway,
                self._config,
                session_id=session_id,
                turn_seq=turn_seq,
                model_id=model_id,
                emit=self._emit,
                cancel=cancel,
                think_id=think_id,
            )
            data = engine.run(
                question=question,
                effort=effort,
                candidate=candidate,
                context=context,
                mode=mode,
            )
        except BtcmCancelled as exc:
            return self._error(ErrorCode.TOOL_CANCELLED, str(exc) or "")
        except BtcmTimeout as exc:
            return self._error(ErrorCode.TOOL_TIMEOUT, str(exc) or "")
        except BtcmError as exc:
            code = (
                ErrorCode.TOOL_INVALID_ARGS if exc.code == "invalid" else ErrorCode.TOOL_BACKEND_ERROR
            )
            return self._error(code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 未预期异常归后端错误，出口去敏
            return self._error(ErrorCode.TOOL_BACKEND_ERROR, redact(str(exc)) or None)

        usage = data.pop("usage", None)
        self._audit(
            "btcm.think",
            effort=effort,
            mode=mode,
            termination=data.get("termination_reason"),
        )
        return ToolResult(ok=True, output=json.dumps(data, ensure_ascii=False), usage=usage)

    def _resolve_model(self, session_id: str) -> str | None:
        session_model: str | None = None
        if session_id:
            try:
                session_model = self._resolve_session_model(session_id)
            except Exception:  # noqa: BLE001 - 会话不可读时退回槽位解析
                session_model = None
        try:
            return resolve_model(self._gateway, session_model, self._config.slot)
        except Exception:  # noqa: BLE001 - 网关不可用时视为未绑定
            return None

    @staticmethod
    def _error(code: ErrorCode, message: str | None = None) -> ToolResult:
        return ToolResult(
            ok=False, error={"code": code.value, "message": message or error_text(code.value)}
        )