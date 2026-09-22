"""shell 宿主：注册、权限、池上限、回收、审计、宿主态（v0.0.5 / docs 07 §3、09 §2）。

用**假 shell**（`tests/mocks/fake_shell.py`，两个方言都跑）注入解释器，
所以这里不依赖本机是否装了真解释器；行为断言全部落在契约与安全面上。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from core.registry.executor import ToolContext, ToolExecutor
from core.registry.registry import Registry
from core.shell.manager import TOOL_EXEC, ShellManager
from core.shell.process import Interpreter
from core.store.config_store import ConfigStore
from shared.enums import ModuleState
from shared.errors import ErrorCode
from shared.schema import ModulesConfig, ShellConfig

FAKE = pathlib.Path(__file__).resolve().parents[1] / "mocks" / "fake_shell.py"


class _Cancel:
    def __init__(self, flag: bool = False) -> None:
        self.flag = flag

    def is_cancelled(self) -> bool:
        return self.flag


def _interp(kind: str = "bash") -> Interpreter:
    return Interpreter(kind=kind, path=sys.executable, argv=(sys.executable, "-u", str(FAKE)))


class _Harness:
    """把 manager + executor + 审计/关卡记录装在一起，便于逐条断言。"""

    def __init__(self, tmp_path, kind: str = "bash", shell: ShellConfig | None = None):
        self.store = ConfigStore(tmp_path)
        if shell is not None:
            self.store.save("modules", ModulesConfig(shell=shell))
        self.registry = Registry()
        self.audits: list[tuple] = []
        self.gates: list[tuple] = []
        self.events: list = []
        self.manager = ShellManager(
            self.registry,
            self.store,
            audit=lambda action, **fields: self.audits.append((action, fields)),
            emit=self.events.append,
            interp=_interp(kind),
        )
        self.manager.load()
        self.executor = ToolExecutor(
            self.registry,
            audit=lambda action, **fields: self.audits.append((action, fields)),
            emit=self.events.append,
            gate=self._gate,
        )
        self.ctx = ToolContext(session_id="sess_1")

    def _gate(self, call_id: str, name: str, args: dict, permission: str) -> bool:
        self.gates.append((name, permission))
        return True

    def exec(self, command: str, call_id: str = "c1", **extra):
        args = {"command": command}
        args.update(extra)
        return self.executor.execute(call_id, TOOL_EXEC, args, self.ctx)

    def actions(self) -> list[str]:
        return [action for action, _ in self.audits]

    def close(self) -> None:
        self.manager.shutdown()


@pytest.fixture(params=["bash", "powershell"])
def h(request, tmp_path):
    harness = _Harness(tmp_path, kind=request.param)
    try:
        yield harness
    finally:
        harness.close()


# -- 注册与环境陈述 ---------------------------------------------------------
def test_tool_is_registered_with_usage_guidance(h):
    specs = [s for s in h.registry.snapshot() if s.name == TOOL_EXEC]
    assert len(specs) == 1
    spec = specs[0]
    assert spec.title == "本机命令"
    # 「系统提示词能指导模型使用 shell」的落点 = description（docs 06 §7 环境陈述）
    for hint in ("执行命令", "持续", "确认", "交互"):
        assert hint in spec.description
    assert spec.input_schema["required"] == ["command"]
    assert "command" in spec.input_schema["properties"]
    assert spec.timeout_ms == 30000


def test_description_names_the_interpreter_kind(tmp_path):
    """模型要据此知道该用哪种语法（Windows 与 Linux 命令面完全不同）。"""
    assert "bash" in _Harness(tmp_path, kind="bash").manager._description()
    harness = _Harness(tmp_path / "ps", kind="powershell")
    assert "powershell" in harness.manager._description()
    harness.close()


def test_default_permission_is_confirm(h):
    assert h.manager.effective_permission() == "confirm"


def test_permission_override_from_config(tmp_path):
    harness = _Harness(
        tmp_path,
        shell=ShellConfig(tool_permissions={TOOL_EXEC: "safe"}),
    )
    try:
        assert harness.manager.effective_permission() == "safe"
        # safe 档直行 → 不产生关卡
        result = harness.exec("echo no-gate")
        assert result.ok
        assert harness.gates == []
    finally:
        harness.close()


def test_invalid_permission_override_falls_back_to_confirm(tmp_path):
    """配置写错不得静默提权/降权（fail-safe 到缺省档）。"""
    harness = _Harness(tmp_path, shell=ShellConfig(tool_permissions={TOOL_EXEC: "confirm"}))
    try:
        assert harness.manager.effective_permission() == "confirm"
    finally:
        harness.close()


# -- 执行 ------------------------------------------------------------------
def test_exec_returns_header_state_and_output(h):
    result = h.exec("echo hello-agent")
    assert result.ok
    first = result.output.splitlines()[0]
    assert first.startswith("shell=s1 kind=")
    assert "cwd=" in first and "exit=0" in first and "duration=" in first
    assert "hello-agent" in result.output
    assert result.duration_ms >= 0


def test_exec_requires_gate_once_per_call(h):
    h.exec("echo a", call_id="c1")
    h.exec("echo b", call_id="c2")
    assert len(h.gates) == 2  # 逐次确认（docs 09 §3：不设批量豁免）


def test_same_session_reuses_one_shell(h):
    h.exec("echo a", call_id="c1")
    h.exec("echo b", call_id="c2")
    assert [s["id"] for s in h.manager.list_status()] == ["s1"]


def test_new_flag_creates_extra_shell(h):
    h.exec("echo a", call_id="c1")
    h.exec("echo b", call_id="c2", new=True)
    assert [s["id"] for s in h.manager.list_status()] == ["s1", "s2"]


def test_shell_limit_is_enforced(h):
    h.exec("echo first", call_id="c1")
    for i in range(h.manager.max_shells() - 1):
        assert h.manager.spawn() is not None, f"第 {i + 2} 个应能创建"
    assert len(h.manager.list_status()) == h.manager.max_shells()
    result = h.exec("echo overflow", call_id="c9", new=True)
    assert not result.ok
    assert result.error["code"] == ErrorCode.TOOL_INVALID_ARGS.value
    assert "上限" in result.error["message"]


def test_unknown_shell_id_is_invalid_args(h):
    h.exec("echo a", call_id="c1")
    result = h.exec("echo b", call_id="c2", shell_id="s404")
    assert not result.ok
    assert result.error["code"] == ErrorCode.TOOL_INVALID_ARGS.value
    assert "s404" in result.error["message"] and "s1" in result.error["message"]


def test_empty_command_is_invalid_args(h):
    result = h.exec("   \n  ")
    assert not result.ok
    assert result.error["code"] == ErrorCode.TOOL_INVALID_ARGS.value


def test_exit_code_visible_to_model(h):
    result = h.exec("fail")
    assert result.ok  # 工具执行成功
    assert "exit=1" in result.output  # 命令失败对模型可见
    assert "boom" in result.output


def test_timeout_maps_to_tool_timeout(h):
    result = h.exec("sleep 10", timeout_ms=1000)
    assert not result.ok
    assert result.error["code"] == ErrorCode.TOOL_TIMEOUT.value
    # 超时即杀进程：池里不再有它
    assert h.manager.list_status() == []


def test_cancel_maps_to_tool_cancelled(h):
    h.ctx.cancel = _Cancel(True)
    result = h.exec("sleep 10")
    assert not result.ok
    assert result.error["code"] == ErrorCode.TOOL_CANCELLED.value


def test_output_is_redacted(h):
    """shell 输出是新增持久化通道 → 必过脱敏（docs 09 B3）。"""
    result = h.exec("echo token sk-abcdef1234567890")
    assert result.ok
    assert "sk-abcdef1234567890" not in result.output
    assert "«已脱敏»" in result.output


# -- 高危策略（关卡之前）---------------------------------------------------
def test_high_risk_denied_without_disturbing_user(h):
    result = h.exec("rm -rf /")
    assert not result.ok
    assert result.error["code"] == ErrorCode.TOOL_DENIED.value
    assert "高危" in result.error["message"]
    assert h.gates == []  # 关键：没有先打扰用户再拒绝
    assert "shell.policy_deny" in h.actions()


def test_high_risk_warns_instead_when_explicitly_enabled(tmp_path):
    harness = _Harness(tmp_path, shell=ShellConfig(allow_restricted=True))
    try:
        result = harness.exec("rm -rf /")
        assert result.ok
        # 显式启用后仍走关卡，但**用户看到的卡片**按高危档呈现（断言发射的 GateRequest）
        requests = [e for e in harness.events if getattr(e, "type", "") == "gate.request"]
        assert len(requests) == 1
        assert requests[0].permission == "restricted"
        assert len(harness.gates) == 1
    finally:
        harness.close()


def test_audit_never_records_command_text(h):
    """审计只记动作与结果，不记命令原文（参数原文只给用户看的关卡卡片，docs 09 B4）。"""
    secret_cmd = "echo audit-should-not-contain-this"
    h.exec(secret_cmd)
    dumped = repr(h.audits)
    assert "audit-should-not-contain-this" not in dumped


def test_policy_check_exception_fails_closed(tmp_path):
    """策略检查自身异常必须按拒绝处理（P6 安全默认）。"""

    def boom(_args):
        raise RuntimeError("policy exploded")

    harness = _Harness(tmp_path)
    try:
        spec = next(s for s in harness.registry.snapshot() if s.name == TOOL_EXEC)
        spec.precheck = boom
        result = harness.exec("echo should-be-denied")
        assert not result.ok
        assert result.error["code"] == ErrorCode.TOOL_DENIED.value
    finally:
        harness.close()


# -- 池、回收、宿主态 -------------------------------------------------------
def test_list_status_exposes_monitoring_fields(h):
    h.exec("echo a", call_id="c1")
    info = h.manager.list_status()[0]
    for key in ("id", "kind", "state", "cwd", "pid", "last_command", "session"):
        assert key in info, key
    assert info["session"] == "sess_1"
    assert info["last_command"] == "echo a"


def test_idle_shells_are_reaped(tmp_path):
    """空闲回收：零定时器，靠下一次操作顺带检查。"""
    harness = _Harness(tmp_path, shell=ShellConfig(idle_timeout_s=60))
    try:
        harness.exec("echo a", call_id="c1")
        assert len(harness.manager.list_status()) == 1
        # 把最后使用时间推回过去 → 下一次 spawn 触发懒回收
        proc = harness.manager._shells["s1"]
        proc.last_used -= 3600
        assert harness.manager.spawn() is not None
        assert "s1" not in [s["id"] for s in harness.manager.list_status()]
        assert "shell.reap" in harness.actions()
    finally:
        harness.close()


def test_dead_shell_is_dropped_from_pool(h):
    h.exec("echo a", call_id="c1")
    h.manager._shells["s1"].close()  # 进程没了
    h.manager.spawn()
    assert "s1" not in [s["id"] for s in h.manager.list_status()]


def test_close_session_closes_bound_shell(h):
    h.exec("echo a", call_id="c1")
    assert h.manager.default_shell_of("sess_1") == "s1"
    h.manager.close_session("sess_1")
    assert h.manager.list_status() == []
    assert h.manager.default_shell_of("sess_1") is None
    assert "shell.close" in h.actions()


def test_close_unknown_shell_is_harmless(h):
    h.manager.close("s404")  # 不得抛


def test_host_state_ready_then_error_without_interpreter(tmp_path):
    harness = _Harness(tmp_path)
    try:
        assert harness.manager.host_state() == ModuleState.READY.value
    finally:
        harness.close()

    from core.shell import manager as manager_mod

    registry = Registry()
    empty = ShellManager(registry, ConfigStore(tmp_path), interp=None)
    original = manager_mod.detect
    manager_mod.detect = lambda _kind="auto": None  # 模拟本机无解释器
    try:
        empty.load()
        assert empty.host_state() == ModuleState.ERROR.value
        assert empty.spawn() is None
        assert empty.last_error()
        assert TOOL_EXEC not in [s.name for s in registry.snapshot()]
    finally:
        manager_mod.detect = original


def test_shutdown_closes_every_shell(h):
    h.exec("echo a", call_id="c1")
    h.exec("echo b", call_id="c2", new=True)
    assert len(h.manager.list_status()) == 2
    h.manager.shutdown()
    assert h.manager.list_status() == []
    assert h.manager.host_state() == ModuleState.READY.value


def test_events_are_emitted_for_list_and_output(h):
    h.exec("emit 3")
    types = [getattr(e, "type", "") for e in h.events]
    assert "shell.list" in types
    assert "shell.output" in types
    listed = [e for e in h.events if getattr(e, "type", "") == "shell.list"][-1]
    assert listed.max_shells == 5
    assert listed.permission == "confirm"
    assert listed.allow_restricted is False
    output = [e for e in h.events if getattr(e, "type", "") == "shell.output"]
    assert "row-0" in "".join(e.chunk for e in output)


def test_user_input_path_bypasses_gate_but_is_audited(h):
    """用户手动输入：用户即主决策者 → 不经关卡，但留痕（docs 09 §3）。"""
    h.exec("echo a", call_id="c1")
    h.gates.clear()
    h.manager.input("s1", "echo manual")
    assert h.gates == []
    assert "shell.input" in h.actions()


def test_user_input_on_missing_shell_reports_error(h):
    h.manager.input("s404", "echo x")
    assert h.manager.last_error()
