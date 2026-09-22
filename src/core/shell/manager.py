"""shell 宿主（spec v0.0.5 §3.3–§3.9）。

职责：解释器探测 → `shell.exec` 注册（权限 = 逐工具覆盖 ⊕ 缺省 confirm）→
全局 shell 池（上限 5）→ 会话默认 shell 绑定 → 高危策略（调用前拒绝）→
懒回收 / 会话删除联动 / 退出全关 → 宿主态同步 `ModuleSupervisor` → 审计。

设计约束（docs 07 §3 / 09 §2）：
- 一切工具同权同审：`shell.exec` 只经注册表 + 七步管线，无旁路；
- **策略拒绝发生在关卡之前**（`ToolSpec.precheck`）：高危命令不该先打扰用户再被拒；
- 审计只记 shell id / 动作 / 结果，**不记命令原文**（参数原文只在关卡卡片给用户，docs 09 B4）；
- 运行态不入盘（docs 03 §8）：shell 列表与输出流都不写 `ymtdata/`。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from shared.enums import ModuleState, Permission
from shared.errors import ErrorCode, error_text
from shared.redact import redact
from shared.schema import ShellConfig

from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec
from core.shell import policy
from core.shell.process import (
    STATE_DEAD,
    Interpreter,
    ShellCancelled,
    ShellDead,
    ShellProcess,
    ShellTimeout,
    default_cwd,
    detect,
)

log = logging.getLogger(__name__)

TOOL_EXEC = "shell.exec"

#: 默认最大 shell 数（用户要求：一个 Agent 最多唤醒 5 个）。
DEFAULT_MAX_SHELLS = 5

STATE_DISABLED = ModuleState.DISABLED.value
STATE_READY = ModuleState.READY.value
STATE_DEGRADED = ModuleState.DEGRADED.value
STATE_ERROR = ModuleState.ERROR.value


class IShellManager(ABC):
    """宿主对外契约（controller 只依赖本接口声明的方法）。

    形状与 `IModuleSupervisor` / `IMcpManager` 同族：声明即契约，替换实现（含测试替身）
    必须能实例化；`tests/static/test_contract_coverage.py` 会把「controller 调了但没声明」
    当场拦下。
    """

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def spawn(self, cwd: str | None = None, session_id: str = "") -> str | None: ...

    @abstractmethod
    def close(self, shell_id: str, reason: str = "user") -> None: ...

    @abstractmethod
    def input(self, shell_id: str, command: str) -> None: ...

    @abstractmethod
    def close_session(self, session_id: str) -> None: ...

    @abstractmethod
    def list_status(self) -> list[dict]: ...

    @abstractmethod
    def max_shells(self) -> int: ...

    @abstractmethod
    def last_error(self) -> str: ...

    @abstractmethod
    def host_state(self) -> str: ...

    @abstractmethod
    def effective_permission(self) -> str: ...

    @abstractmethod
    def allow_restricted(self) -> bool: ...

    @abstractmethod
    def shutdown(self) -> None: ...


def _result_error(code: ErrorCode, message: str | None = None) -> ToolResult:
    return ToolResult(ok=False, error={"code": code.value, "message": message or error_text(code.value)})


class ShellManager(IShellManager):
    def __init__(
        self,
        registry: Registry,
        config_store: Any,
        audit: Callable[..., None] | None = None,
        emit: Callable[[Any], None] | None = None,
        interp: Interpreter | None = None,
    ) -> None:
        self._registry = registry
        self._config_store = config_store
        self._audit = audit or (lambda *a, **k: None)
        self._emit = emit or (lambda *a, **k: None)
        self._config = ShellConfig()
        self._interp = interp
        self._detected = interp is not None
        self._error = ""
        self._shells: dict[str, ShellProcess] = {}
        self._order: list[str] = []  # 稳定展示顺序
        self._session_shell: dict[str, str] = {}
        self._counter = 0
        self._registered = False

    # ---------- 配置与注册 ----------
    def load(self) -> None:
        """读配置 + 探测解释器（**不起进程**：保持启动零子进程）。"""
        try:
            self._config = self._config_store.load("modules").shell
        except Exception:  # noqa: BLE001 - 配置异常回退默认，不阻断启动
            log.exception("shell 配置读取失败，回退默认")
            self._config = ShellConfig()
        if not self._detected:
            self._interp = detect(self._config.kind)
            self._detected = True
        if self._interp is None:
            self._error = (
                f"未找到可用 shell 解释器（kind={self._config.kind}）："
                "请确认本机已安装对应解释器，或在 modules.json 的 shell.kind 中指定。"
            )
            self._unregister()
            return
        self._error = ""
        self._register()

    def _description(self) -> str:
        """给模型的一句话（同时是环境陈述行 —— 控制长度，勿超 `_tool_lines` 的截断线）。

        这段文字就是「系统提示词指导模型使用 shell」的全部内容：说清语法、状态是否持续、
        怎么复用/另开、确认语义、以及最容易踩的坑（交互式命令会卡到超时）。
        """
        interp = self._interp
        kind = interp.kind if interp else "?"
        syntax = "PowerShell" if interp and interp.dialect == "ps" else "POSIX shell"
        return (
            f"在本机 {kind}（{syntax} 语法）执行命令，取回输出、退出码与工作目录。"
            "变量与当前目录在多次调用之间持续；省略 shell_id 即复用本会话默认 shell，"
            f"new=true 另开一个（全应用最多 {self._max_shells()} 个）。命令可用换行写多行脚本。"
            "默认需用户逐次确认；勿用需要交互输入的命令（会一直等到超时）。"
        )

    def _register(self) -> None:
        if self._registered:
            self._unregister()
        spec = ToolSpec(
            name=TOOL_EXEC,
            title="本机命令",
            description=self._description(),
            permission=self._permission(),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令（本机解释器语法，可含换行）",
                    },
                    "shell_id": {
                        "type": "string",
                        "description": "复用已有 shell 的 id（见上次结果的 shell= 字段）",
                    },
                    "new": {
                        "type": "boolean",
                        "description": "新建一个 shell（受上限约束）",
                    },
                    "cwd": {"type": "string", "description": "仅新建时生效：初始工作目录"},
                    "timeout_ms": {"type": "integer", "description": "本次执行的超时毫秒"},
                },
                "required": ["command"],
            },
            timeout_ms=self._config.timeout_ms,
            availability=lambda: self._interp is not None,
            precheck=self._precheck,
        )
        self._registry.register(spec, self._handle_exec)
        self._registered = True
        self._emit_list()

    def _unregister(self) -> None:
        if self._registered:
            self._registry.unregister(TOOL_EXEC)
            self._registered = False
        self._emit_list()

    def _permission(self) -> Permission:
        """有效权限 = modules.json 的逐工具覆盖 ⊕ 缺省 confirm（docs 09 §2）。"""
        raw = (self._config.tool_permissions or {}).get(TOOL_EXEC)
        if not raw:
            return Permission.CONFIRM
        try:
            return Permission(raw)
        except ValueError:
            return Permission.CONFIRM

    def effective_permission(self) -> str:
        return self._permission().value

    def allow_restricted(self) -> bool:
        """高危档是否已显式启用（默认关，docs 09 §2）。"""
        return bool(self._config.allow_restricted)

    def _max_shells(self) -> int:
        try:
            return max(1, int(self._config.max_shells))
        except (TypeError, ValueError):
            return DEFAULT_MAX_SHELLS

    def max_shells(self) -> int:
        """池上限（对外可读，供界面展示「最多 N 个」）。"""
        return self._max_shells()

    # ---------- 高危策略（关卡之前）----------
    def _precheck(self, args: dict) -> tuple[str, str] | None:
        """返回 (`deny`|`warn`, 说明)；命中高危清单时决定拒绝或仅标注。

        默认（`allow_restricted=false`）**策略直接拒绝**并留痕；显式启用后放行，
        但以 `warn` 让关卡卡片标注「高危」（docs 09 §2 restricted 档）。
        """
        label = policy.classify(str(args.get("command") or ""))
        if not label:
            return None
        if self.allow_restricted():
            return ("warn", f"命中高危清单（{label}），已配置为允许执行")
        self._audit("shell.policy_deny", tool=TOOL_EXEC, label=label)
        return (
            "deny",
            f"该命令命中高危清单（{label}），已被策略拒绝（docs 09 §2 restricted 档默认关闭）。"
            "如确需执行，请由用户在 modules.json 中把 shell.allow_restricted 置为 true。",
        )

    # ---------- 池 ----------
    def _reap(self) -> None:
        """懒回收（零定时器）：空闲超阈值的 shell 在此关闭。"""
        try:
            idle = max(60, int(self._config.idle_timeout_s))
        except (TypeError, ValueError):
            idle = 600
        now = time.time()
        for shell_id in list(self._order):
            proc = self._shells.get(shell_id)
            if proc is None:
                continue
            if not proc.alive:
                self.close(shell_id, reason="exited")
                continue
            if now - proc.last_used > idle:
                self._audit("shell.reap", id=shell_id, idle_s=idle)
                self.close(shell_id, reason="idle")

    def _new_id(self) -> str:
        self._counter += 1
        return f"s{self._counter}"

    def spawn(self, cwd: str | None = None, session_id: str = "") -> str | None:
        """新建 shell；成功返回 id，失败返回 None（原因见 `last_error()`）。"""
        self._reap()
        if self._interp is None:
            self._error = "未找到可用 shell 解释器。"
            return None
        if len(self._shells) >= self._max_shells():
            self._error = (
                f"已达 shell 上限（{self._max_shells()} 个）：请先关闭不用的 shell（终端页可关闭）。"
            )
            return None
        shell_id = self._new_id()
        workdir = cwd or self._config.cwd or default_cwd()
        proc = ShellProcess(
            self._interp,
            workdir,
            shell_id=shell_id,
            emit=lambda chunk, sid=shell_id: self._emit_output(sid, chunk),
        )
        try:
            proc.start()
        except ShellDead as exc:
            self._error = redact(str(exc)) or "shell 启动失败。"
            self._audit("shell.spawn", id=shell_id, kind=self._interp.kind, ok=False)
            self._emit_list()
            return None
        self._shells[shell_id] = proc
        self._order.append(shell_id)
        if session_id:
            self._session_shell.setdefault(session_id, shell_id)
        self._error = ""
        self._audit("shell.spawn", id=shell_id, kind=self._interp.kind, ok=True)
        self._emit_list()
        return shell_id

    def close(self, shell_id: str, reason: str = "user") -> None:
        proc = self._shells.get(shell_id)
        self._drop(shell_id)
        if proc is None:
            self._emit_list()
            return
        proc.close()
        self._audit("shell.close", id=shell_id, reason=reason)
        self._emit_list()

    def close_session(self, session_id: str) -> None:
        """会话删除时关闭其派生的默认 shell（避免悬挂进程）。"""
        bound = self._session_shell.pop(session_id, "")
        if bound:
            self.close(bound, reason="session_deleted")
        else:
            self._emit_list()

    def default_shell_of(self, session_id: str) -> str | None:
        return self._session_shell.get(session_id)

    def _drop(self, shell_id: str) -> None:
        """把某个 shell 从池里摘掉（进程已被判死，状态不再可信）。"""
        self._shells.pop(shell_id, None)
        if shell_id in self._order:
            self._order.remove(shell_id)
        for session_id, bound in list(self._session_shell.items()):
            if bound == shell_id:
                self._session_shell.pop(session_id, None)

    def _resolve(self, args: dict, session_id: str) -> tuple[str | None, str]:
        """解析本次调用落在哪个 shell 上；返回 (shell_id, 错误说明)。"""
        requested = str(args.get("shell_id") or "").strip()
        want_new = bool(args.get("new"))
        if want_new:
            created = self.spawn(cwd=args.get("cwd"), session_id=session_id)
            return created, ("" if created else self._error)
        if requested:
            if requested not in self._shells:
                return None, f"未知的 shell_id：{requested}（可用：{', '.join(self._order) or '无'}）"
            return requested, ""
        existing = self._session_shell.get(session_id) or ""
        if existing and existing in self._shells and self._shells[existing].alive:
            return existing, ""
        created = self.spawn(cwd=args.get("cwd"), session_id=session_id)
        return created, ("" if created else self._error)

    # ---------- 工具执行 ----------
    def _handle_exec(self, args: dict, ctx: Any = None) -> ToolResult:
        self._reap()
        command = str(args.get("command") or "").strip("\n")
        if not command.strip():
            return _result_error(ErrorCode.TOOL_INVALID_ARGS, "命令不能为空。")
        session_id = str(getattr(ctx, "session_id", "") or "")
        shell_id, why = self._resolve(args, session_id)
        if shell_id is None:
            return _result_error(ErrorCode.TOOL_INVALID_ARGS, why or "无法取得可用 shell。")
        proc = self._shells[shell_id]

        timeout_ms = self._timeout_of(args)
        cancel = getattr(ctx, "cancel", None)
        try:
            run = proc.run(command, timeout_ms=timeout_ms, cancel=cancel)
        except ShellCancelled as exc:
            self._drop(shell_id)  # 取消/超时都会杀掉进程 → 池里不该留一个死壳
            self._audit("shell.exec", id=shell_id, ok=False, code=ErrorCode.TOOL_CANCELLED.value)
            self._emit_list()
            return _result_error(ErrorCode.TOOL_CANCELLED, redact(str(exc)) or "")
        except ShellTimeout as exc:
            self._drop(shell_id)
            self._audit("shell.exec", id=shell_id, ok=False, code=ErrorCode.TOOL_TIMEOUT.value)
            self._emit_list()
            return _result_error(ErrorCode.TOOL_TIMEOUT, redact(str(exc)) or "")
        except ShellDead as exc:
            self._drop(shell_id)
            self._audit("shell.exit", id=shell_id, ok=False)
            self._emit_list()
            return _result_error(ErrorCode.TOOL_BACKEND_ERROR, redact(str(exc)) or "")
        except Exception as exc:  # noqa: BLE001 - 后端异常归码，不留栈于出口
            log.exception("shell 执行异常")
            self._audit("shell.exec", id=shell_id, ok=False, code=ErrorCode.TOOL_BACKEND_ERROR.value)
            return _result_error(ErrorCode.TOOL_BACKEND_ERROR, redact(str(exc)) or "")

        self._audit("shell.exec", id=shell_id, ok=True, exit=run.exit_code)
        self._emit_list()
        output = self._compose(shell_id, proc, run)
        return ToolResult(ok=True, output=output, duration_ms=run.duration_ms)

    def _timeout_of(self, args: dict) -> int:
        raw = args.get("timeout_ms")
        try:
            value = int(raw) if raw is not None else int(self._config.timeout_ms)
        except (TypeError, ValueError):
            value = 30000
        return max(1000, min(value, 600000))

    @staticmethod
    def _compose(shell_id: str, proc: ShellProcess, run) -> str:
        """回给模型的结果：先给「有关的情况」，再给输出（一行头部，模型无需再问一次）。

        输出**必过 `redact`**：shell 输出是新增的持久化通道（经 `tool.result` 落
        `events.jsonl`），按铁律须接入脱敏（docs 09 B3 + 项目约定）。
        副作用已知：含 `sk-`/`api_key=` 形态的正常文本也会被打码（`shared.redact` 的取舍）。
        """
        header = (
            f"shell={shell_id} kind={proc.interp.kind} cwd={run.cwd} "
            f"exit={run.exit_code} duration={run.duration_ms}ms"
        )
        body = redact(run.output or "") or ""
        if len(body) > 200000:  # 极端情况兜底（常规路径由执行器外置处理）
            body = body[:200000] + "\n…（输出过长，已截断）"
        return header + "\n---\n" + body

    # ---------- 用户手动使用 ----------
    def input(self, shell_id: str, command: str) -> None:
        """用户直接在终端页送命令：不经关卡（用户即主决策者），但记 audit。"""
        proc = self._shells.get(shell_id)
        if proc is None:
            self._error = "该 shell 已不存在。"
            self._emit_list()
            return
        text = str(command or "").strip("\n")
        if not text.strip():
            return
        self._audit("shell.input", id=shell_id, ok=True)  # 不记命令原文（docs 09 B4）
        try:
            proc.run(text, timeout_ms=self._config.timeout_ms)
        except (ShellCancelled, ShellTimeout, ShellDead) as exc:
            log.info("用户输入的 shell 命令未正常结束：%s", type(exc).__name__)
        finally:
            self._emit_list()

    # ---------- 查询与状态 ----------
    def last_error(self) -> str:
        return self._error

    def list_status(self) -> list[dict]:
        """池快照（供终端页监控）：附上「所属会话」，便于用户分辨是谁开的。"""
        reverse = {shell_id: session for session, shell_id in self._session_shell.items()}
        out: list[dict] = []
        for shell_id in self._order:
            proc = self._shells.get(shell_id)
            if proc is None:
                continue
            info = proc.info()
            info["session"] = reverse.get(shell_id, "")
            out.append(info)
        return out

    def host_state(self) -> str:
        """宿主态：无 shell → ready（能力可用、按需起）；解释器缺失 → error。"""
        if self._interp is None:
            return STATE_ERROR
        if not self._shells:
            return STATE_READY
        alive = [p for p in self._shells.values() if p.alive]
        if not alive:
            return STATE_ERROR
        if len(alive) < len(self._shells):
            return STATE_DEGRADED
        return STATE_READY

    def shutdown(self) -> None:
        for shell_id in list(self._order):
            self.close(shell_id, reason="shutdown")

    # ---------- 事件 ----------
    def _emit_list(self) -> None:
        from shared.envelope import ShellList

        self._emit(
            ShellList(
                shells=self.list_status(),
                max_shells=self._max_shells(),
                permission=self.effective_permission(),
                allow_restricted=self.allow_restricted(),
            )
        )

    def _emit_output(self, shell_id: str, chunk: str) -> None:
        from shared.envelope import ShellOutput

        self._emit(ShellOutput(id=shell_id, chunk=chunk))


__all__ = [
    "DEFAULT_MAX_SHELLS",
    "IShellManager",
    "STATE_DEAD",
    "TOOL_EXEC",
    "ShellManager",
]
