"""持久 shell 进程的协议与边界（v0.0.5 / spec §3.1）。

全部用**假 shell**（`tests/mocks/fake_shell.py`）：同样的线格式，但不需要真解释器 ——
秒级、跨平台、且能构造超时/取消/进程自退这些极端情形。两个方言都测：
`kind="bash"` 走 posix 行尾命令，`kind="powershell"` 走 ASCII base64 包装。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from core.shell.process import (
    STATE_DEAD,
    Interpreter,
    ShellCancelled,
    ShellDead,
    ShellProcess,
    ShellTimeout,
)

FAKE = pathlib.Path(__file__).resolve().parents[1] / "mocks" / "fake_shell.py"

#: 两个方言都跑：同一组断言应在两条协议路径上同时成立。
DIALECT_KINDS = ["bash", "powershell"]


class _Cancel:
    """具名弱接口的取消令牌替身（只要求 `is_cancelled()`，与 `ToolContext.cancel` 同形）。"""

    def __init__(self, flag: bool = False) -> None:
        self.flag = flag

    def is_cancelled(self) -> bool:
        return self.flag


def _make(kind: str, cwd: pathlib.Path, emit=None) -> ShellProcess:
    interp = Interpreter(kind=kind, path=sys.executable, argv=(sys.executable, "-u", str(FAKE)))
    proc = ShellProcess(interp, str(cwd), shell_id="t1", emit=emit)
    proc.start()
    return proc


@pytest.fixture(params=DIALECT_KINDS)
def shell(request, tmp_path):
    proc = _make(request.param, tmp_path)
    try:
        yield proc
    finally:
        proc.close()


@pytest.mark.parametrize("kind", DIALECT_KINDS)
def test_start_reports_initial_cwd(kind, tmp_path):
    proc = _make(kind, tmp_path)
    try:
        assert proc.alive
        assert proc.state == "ready"
        assert pathlib.Path(proc.cwd).name == tmp_path.name
        assert proc.pid
    finally:
        proc.close()


def test_echo_returns_output_and_exit_zero(shell):
    run = shell.run("echo hello")
    assert "hello" in run.output
    assert run.exit_code == 0
    assert run.duration_ms >= 0
    assert run.cwd


def test_output_never_leaks_marker(shell):
    """哨兵是内部协议，绝不能出现在回给模型的输出里。"""
    run = shell.run("echo visible-text")
    assert "visible-text" in run.output
    assert "__YMT_" not in run.output


def test_utf8_round_trip(shell):
    """中文命令与中文输出都不能被编码问题破坏（本机实测过 PS 5.1 的坑，见 process docstring）。"""
    run = shell.run('echo 中文参数')
    assert "中文参数" in run.output
    run = shell.run("utf8")
    assert "中文回显" in run.output


def test_ansi_is_passed_through_untouched(shell):
    """ANSI 转义要原样保留 —— 渲染层负责上色（gui/widgets/render）。"""
    run = shell.run("ansi")
    assert "\x1b[31mred\x1b[0m" in run.output


@pytest.mark.parametrize("kind", DIALECT_KINDS)
def test_exit_code_is_reported(kind, tmp_path):
    proc = _make(kind, tmp_path)
    try:
        assert proc.run("exit 3").exit_code == 3
        assert proc.run("fail").exit_code == 1
        # 失败之后不得被上一次的码污染（PS 的 $LASTEXITCODE 是粘滞的，曾踩过）
        assert proc.run("echo clean").exit_code == 0
    finally:
        proc.close()


def test_multiline_command_posix(shell):
    """多行脚本：posix 由解释器自行累积；本假 shell 按整段处理。"""
    run = shell.run("multi")
    assert run.output.count("\n") >= 2
    assert "line-1" in run.output and "line-3" in run.output


def test_state_persists_across_calls(shell, tmp_path):
    """状态持续是「唤醒」的全部价值：cd 之后 cwd 跟着走。"""
    target = tmp_path / "sub"
    target.mkdir()
    run = shell.run(f'cd "{target}"')
    assert run.exit_code == 0
    assert pathlib.Path(shell.cwd) == target
    assert str(target) in shell.run("pwd").output
    assert shell.info()["cwd"] == str(target)


def test_timeout_kills_shell(shell):
    """超时 → 杀掉进程（状态不再可信，宁可重建）。"""
    with pytest.raises(ShellTimeout):
        shell.run("sleep 5", timeout_ms=1000)
    assert not shell.alive
    assert shell.state == STATE_DEAD
    with pytest.raises(ShellDead):
        shell.run("echo after-timeout")


def test_cancel_kills_shell(shell):
    with pytest.raises(ShellCancelled):
        shell.run("sleep 5", timeout_ms=30000, cancel=_Cancel(True))
    assert not shell.alive


def test_cancelled_after_finish_is_not_triggered(shell):
    """未取消的令牌不得误杀：正常命令照常完成。"""
    run = shell.run("echo ok", timeout_ms=5000, cancel=_Cancel(False))
    assert "ok" in run.output


def test_crash_surfaces_as_dead(shell):
    with pytest.raises(ShellDead):
        shell.run("crash")
    assert not shell.alive


def test_emit_receives_output_chunks(tmp_path):
    """监视流：合帧后仍必须把输出送达（终端页靠它实时显示）。"""
    chunks: list[str] = []
    proc = _make("bash", tmp_path, emit=chunks.append)
    try:
        proc.run("emit 25")
        proc.run("echo tail-marker")
        joined = "".join(chunks)
        assert "row-0" in joined and "row-24" in joined
        assert "tail-marker" in joined
    finally:
        proc.close()


def test_close_is_idempotent(tmp_path):
    proc = _make("bash", tmp_path)
    proc.close()
    assert not proc.alive
    proc.close()  # 再关一次不得抛
    assert proc.state == STATE_DEAD
