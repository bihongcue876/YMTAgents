"""shell 端到端（v0.0.5）：真解释器 + controller 入口（不绕信封层，rev5 教训）。

真解释器只在少数用例里用（启动子进程有成本），且探测不到就 `skip` ——
环境依赖不该把测试变红。
"""

from __future__ import annotations

import pytest

from app import bootstrap as bootstrap_mod
from app import paths
from core.registry.executor import ToolContext
from core.shell.manager import TOOL_EXEC
from core.shell.process import ShellProcess, detect
from shared.envelope import ShellClose, ShellInput, ShellRefresh, ShellSpawn
from tests.mocks.gateway import MockGateway


def _real_interp():
    interp = detect("auto")
    if interp is None:
        pytest.skip("本机没有可用 shell 解释器")
    return interp


def _boot(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=lambda _store: MockGateway())


def _collect(ctx) -> list:
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    return events


# -- 真解释器 -----------------------------------------------------------------
def test_real_shell_runs_command_and_reports_state(tmp_path):
    """真解释器：命令、退出码、cwd、中文双向都要对。"""
    interp = _real_interp()
    proc = ShellProcess(interp, str(tmp_path), shell_id="e2e")
    try:
        proc.start()
        assert proc.alive
        run = proc.run('echo hello-e2e')
        assert run.exit_code == 0
        assert "hello-e2e" in run.output
        # 中文往返（Windows PS 5.1 的编码坑在此钉住）
        run = proc.run('echo 中文-e2e')
        assert "中文-e2e" in run.output
        run = proc.run("exit_probe_missing")
        assert run.exit_code != 0  # 未知命令必须给出非零码
        assert run.output.strip()
    finally:
        proc.close()


def test_real_shell_supports_multiline_script(tmp_path):
    """多行脚本：两个方言都必须可用（PS 的输入是 ASCII 包装，见 process 模块说明）。"""
    interp = _real_interp()
    proc = ShellProcess(interp, str(tmp_path), shell_id="e2e-ml")
    try:
        proc.start()
        if interp.dialect == "ps":
            run = proc.run('$a = 1\n$a = $a + 1\nWrite-Output "value=$a"')
        else:
            run = proc.run('a=1\na=$((a + 1))\necho "value=$a"')
        assert run.exit_code == 0
        assert "value=2" in run.output
    finally:
        proc.close()


def test_real_shell_keeps_state_across_calls(tmp_path):
    interp = _real_interp()
    sub = tmp_path / "nested"
    sub.mkdir()
    proc = ShellProcess(interp, str(tmp_path), shell_id="e2e-state")
    try:
        proc.start()
        proc.run(f'cd "{sub}"')
        assert str(sub) in proc.cwd
        assert str(sub) in proc.run("pwd" if interp.dialect == "posix" else "Write-Output $PWD.Path").output
    finally:
        proc.close()


# -- 装配与控制入口 -----------------------------------------------------------
def test_shell_list_pushed_on_initial_state(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        ctx.controller.push_initial_state()
        listed = [e for e in events if e.type == "shell.list"]
        assert listed, "首屏必须推送 shell 列表"
        assert listed[0].shells == []
        assert listed[0].max_shells == 5
        assert listed[0].permission == "confirm"
        assert listed[0].allow_restricted is False
    finally:
        ctx.worker.stop()


def test_shell_host_state_synced_into_supervisor(tmp_path, monkeypatch, qapp):
    ctx = _boot(tmp_path, monkeypatch)
    try:
        ctx.controller.refresh_modules()
        state = ctx.supervisor.get_states()["shell"]
        assert state == ("ready" if detect("auto") else "error")
    finally:
        ctx.worker.stop()


def test_shell_requests_round_trip_through_controller(tmp_path, monkeypatch, qapp):
    """从 `controller.handle` 入口走一遍：spawn → input → close（rev5 教训：不得绕信封层）。"""
    _real_interp()
    ctx = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        ctx.controller.handle(ShellSpawn())
        listed = [e for e in events if e.type == "shell.list"][-1]
        assert [s["id"] for s in listed.shells] == ["s1"]
        assert listed.shells[0]["kind"]

        ctx.controller.handle(ShellInput(id="s1", command="echo manual-input"))
        outputs = "".join(e.chunk for e in events if e.type == "shell.output")
        assert "manual-input" in outputs

        ctx.controller.handle(ShellRefresh())
        assert [e for e in events if e.type == "shell.list"]

        ctx.controller.handle(ShellClose(id="s1"))
        assert [e for e in events if e.type == "shell.list"][-1].shells == []
    finally:
        ctx.worker.stop()


def test_shell_tool_reaches_model_as_function_definition(tmp_path, monkeypatch, qapp):
    """工具经原生 function calling 下发（能力面），描述进环境陈述（指导面）。"""
    _real_interp()
    ctx = _boot(tmp_path, monkeypatch)
    try:
        payloads = ctx.executor.tool_payloads()
        names = [p["function"]["name"] for p in payloads]
        assert TOOL_EXEC in names
        payload = next(p for p in payloads if p["function"]["name"] == TOOL_EXEC)
        assert payload["function"]["parameters"]["required"] == ["command"]

        lines = ctx.agent._tool_lines(payloads)
        assert any(line.startswith(TOOL_EXEC + " — ") for line in lines)
        text = next(line for line in lines if line.startswith(TOOL_EXEC))
        assert "持续" in text  # 状态持续
        assert "确认" in text  # 逐次确认
    finally:
        ctx.worker.stop()


def test_env_statement_includes_tool_guidance(tmp_path, monkeypatch, qapp):
    """系统提示词的「环境声明」必须带上工具的用法说明（v0.0.5 唯一提示词改动点）。"""
    _real_interp()
    ctx = _boot(tmp_path, monkeypatch)
    try:
        payloads = ctx.executor.tool_payloads()
        lines = ctx.agent._tool_lines(payloads)
        assert lines, "有可见工具时环境陈述不得为空"
        assert all(" — " in line for line in lines)
        # 描述过长要截断（token 成本有界）
        assert all(len(line) <= 200 for line in lines)
    finally:
        ctx.worker.stop()


def test_executor_runs_real_shell_with_gate(tmp_path, monkeypatch, qapp):
    """经完整七步管线执行一次真命令（含 confirm 关卡）。"""
    _real_interp()
    ctx = _boot(tmp_path, monkeypatch)
    decided: list = []
    try:
        ctx.executor.set_gate(lambda call_id, name, args, permission: decided.append(name) or True)
        ctx.controller.handle(ShellSpawn())
        session_id = ctx.controller.current_session_id or "sess_e2e"
        result = ctx.executor.execute(
            "call_1",
            TOOL_EXEC,
            {"command": "echo pipeline-ok"},
            ToolContext(session_id=session_id),
        )
        assert decided == [TOOL_EXEC]
        assert result.ok
        assert "echo pipeline-ok" not in result.output.splitlines()[0]
        assert "pipeline-ok" in result.output
        assert "exit=0" in result.output
    finally:
        ctx.controller.handle(ShellClose(id="s1"))
        ctx.worker.stop()


def test_refresh_reloads_config_without_restart(tmp_path, monkeypatch, qapp):
    """`shell.refresh` = 重读配置 → 权限档/上限立刻反映到工具与界面（文件即配置，docs 01 §7.3）。"""
    _real_interp()
    ctx = _boot(tmp_path, monkeypatch)
    events = _collect(ctx)
    try:
        assert ctx.shell_manager.effective_permission() == "confirm"
        # 用户手改配置文件：shell.exec 降为 safe、高危档显式启用
        from shared.schema import ModulesConfig, ShellConfig

        ctx.config_store.save(
            "modules",
            ModulesConfig(
                shell=ShellConfig(
                    tool_permissions={TOOL_EXEC: "safe"}, allow_restricted=True, max_shells=3
                )
            ),
        )
        ctx.controller.handle(ShellRefresh())
        assert ctx.shell_manager.effective_permission() == "safe"
        assert ctx.shell_manager.allow_restricted() is True
        spec = next(s for s in ctx.registry.snapshot() if s.name == TOOL_EXEC)
        assert spec.permission.value == "safe"  # 注册档位跟着变（不只是展示）
        listed = [e for e in events if e.type == "shell.list"][-1]
        assert listed.permission == "safe" and listed.max_shells == 3
        assert listed.allow_restricted is True
    finally:
        ctx.worker.stop()


def test_high_risk_is_denied_through_controller_path(tmp_path, monkeypatch, qapp):
    """高危命令在真实装配下同样被策略拒绝，且不打扰用户。"""
    _real_interp()
    ctx = _boot(tmp_path, monkeypatch)
    gates: list = []
    try:
        ctx.executor.set_gate(lambda *args: gates.append(args) or True)
        ctx.controller.handle(ShellSpawn())
        result = ctx.executor.execute(
            "call_risk",
            TOOL_EXEC,
            {"command": "rm -rf /"},
            ToolContext(session_id="sess_e2e"),
        )
        assert not result.ok
        assert result.error["code"] == "tool_denied"
        assert gates == []
    finally:
        ctx.controller.handle(ShellClose(id="s1"))
        ctx.worker.stop()
