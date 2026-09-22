"""持久本机 shell 进程（spec v0.0.5 §3.1/§3.2）。

**线格式（本机实测确定，勿凭猜改写）**
------------------------------------------------
两个方言，各自都经过真机验证（PS 5.1 / bash 5.x）：

1. `posix`（bash/sh）：命令原文按行写入，随后一行尾命令
   `printf '<marker>%s|%s\\n' "$?" "$PWD"`，最后补一个空行。
   - `$?` 每次命令都会更新，故退出码直接取自它；
   - 行内多语句块（`if … then … fi`）由 bash 自行累积，实测可用。

2. `ps`（pwsh / Windows PowerShell）：**输出侧**在 bootstrap 设
   `[Console]::OutputEncoding=UTF8`（实测：不设则 PS 自带的错误文本是 GBK 乱码；
   设了则成功流与错误流都是 UTF-8，中文可读）。但该设置会让 PS 用非 UTF-8
   解码 stdin，中文命令会被破坏 —— 故**输入侧全程 ASCII**：整段脚本
   （前缀 + 用户命令 + 尾行）先 base64 编码，会话内解码成字符串后**点源**执行
   （`.` + `scriptblock::Create`，当前作用域 → 变量/cwd/函数持续有效）。
   - 尾行必须与用户命令**同处一个脚本块**，否则 `$?` 反映的是包装语句而非用户命令；
   - 退出码语义：`$LASTEXITCODE` 只在原生命令后更新且**粘滞**，故前缀先记旧值，
     尾行按「变了 → 取新值；没变且有错 → 1；否则 0」判定。

**行为约定**
- 命令原文与哨兵之间用一次性随机 nonce 隔离，避免输出里出现同形态字符串被误判为定界；
- 取消 / 超时 → **杀掉进程**：状态已不可信，宁可下次重建，也不假装还干净；
- `stderr` 合并进 `stdout`（等价终端的 `2>&1`）：省一个线程、少一套合并逻辑。
"""

from __future__ import annotations

import base64
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from locale import getpreferredencoding
from typing import Callable

log = logging.getLogger(__name__)

#: 方言：命令如何送进去、退出码与 cwd 如何取回来。
DIALECT_PS = "ps"
DIALECT_POSIX = "posix"

#: kind → 方言。kind 同时也是用户在 modules.json 里能写的取值。
DIALECT_OF: dict[str, str] = {
    "pwsh": DIALECT_PS,
    "powershell": DIALECT_PS,
    "bash": DIALECT_POSIX,
    "sh": DIALECT_POSIX,
}

#: 平台默认候选链（先到先用）。Windows 无 pwsh 时回退系统自带 Windows PS 5.1；
#: 不提供 cmd.exe 兜底：它是第三套哨兵协议，收益低而维护面大（spec §9.5）。
CANDIDATES: dict[str, tuple[str, ...]] = {
    "win32": ("pwsh", "powershell"),
    "posix": ("bash", "sh"),
}

STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_BUSY = "busy"
STATE_DEAD = "dead"

#: 单次读取轮询间隔（同时是取消/超时的检查粒度）。
POLL_S = 0.05

#: 监视流合帧：满 N 行或隔 N 秒发一次，避免逐行刷 UI。
EMIT_LINES = 20
EMIT_INTERVAL_S = 0.08


class ShellError(Exception):
    """shell 层面的失败基类。"""


class ShellDead(ShellError):
    """shell 进程不存在或已退出。"""


class ShellTimeout(ShellError):
    """命令超出本次执行超时。"""


class ShellCancelled(ShellError):
    """命令被取消（用户中断 / 回合取消）。"""


@dataclass(frozen=True)
class Interpreter:
    kind: str
    path: str
    argv: tuple[str, ...]

    @property
    def dialect(self) -> str:
        return DIALECT_OF[self.kind]


@dataclass
class ShellRun:
    output: str = ""
    exit_code: int = 0
    cwd: str = ""
    duration_ms: int = 0


def _which(name: str) -> str | None:
    if os.name == "nt" and not name.lower().endswith(".exe"):
        return shutil.which(name + ".exe") or shutil.which(name)
    return shutil.which(name)


def default_cwd() -> str:
    """默认工作目录：用户主目录（取不到时用当前目录）。"""
    try:
        return os.path.expanduser("~")
    except Exception:  # noqa: BLE001 - 极端环境下退回 cwd
        return os.getcwd()


def detect(kind: str = "auto") -> Interpreter | None:
    """探测解释器；`auto` 时按平台候选链。找不到返回 None（调用方据此报可读原因）。"""
    if kind and kind != "auto":
        path = _which(kind)
        return _interpreter(kind, path) if path else None
    names = CANDIDATES.get("win32" if os.name == "nt" else "posix", ())
    for name in names:
        path = _which(name)
        if path:
            return _interpreter(name, path)
    return None


def _interpreter(kind: str, path: str) -> Interpreter:
    if DIALECT_OF[kind] == DIALECT_PS:
        argv = (
            path,
            "-NoLogo",
            "-NoProfile",  # 不加载用户 profile：启动快、输出可预测
            "-NonInteractive",  # 不弹交互提示（否则会静默挂住）
            "-Command",
            "-",  # 从 stdin 读命令（实测逐行有效）
        )
    else:
        argv = (path, "--noprofile", "--norc")
    return Interpreter(kind=kind, path=path, argv=argv)


def _decode(raw: bytes) -> str:
    """逐行解码：优先 UTF-8，失败回退系统首选编码（防御性，实测主路径均为 UTF-8）。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode(getpreferredencoding(False), "replace")
        except Exception:  # noqa: BLE001 - 编码名不可用也不能让读线程死掉
            return raw.decode("utf-8", "replace")


@dataclass
class _Protocol:
    """方言的协议文本生成器。"""

    dialect: str
    marker: str
    ps_bootstrap: str = ""
    _prefix: str = ""
    _trailer: str = ""

    @property
    def start_command(self) -> str:
        return self.ps_bootstrap

    def payload(self, command: str) -> str:
        body = command.rstrip("\n")
        if self.dialect == DIALECT_PS:
            script = f"{self._prefix}\n{body}\n{self._trailer}"
            encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
            return (
                "$__ymt_c=[Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{encoded}'));"
                ". ([scriptblock]::Create($__ymt_c))"
            )
        return body + "\n" + self._trailer


def make_protocol(dialect: str) -> _Protocol:
    marker = "__YMT_" + uuid.uuid4().hex[:8] + "__"
    if dialect == DIALECT_PS:
        prefix = "$__ymt_pre=$LASTEXITCODE"
        trailer = (
            "$__ymt_ok=$?;$__ymt_post=$LASTEXITCODE;"
            "if($__ymt_post -ne $__ymt_pre){$__ymt_x=$__ymt_post}"
            "elseif($__ymt_ok){$__ymt_x=0}else{$__ymt_x=1};"
            "if($null -eq $__ymt_x){$__ymt_x=0};"
            f'Write-Output ("{marker}"+$__ymt_x+"|"+$PWD.Path)'
        )
        return _Protocol(
            dialect=dialect,
            marker=marker,
            ps_bootstrap=(
                "$ProgressPreference='SilentlyContinue';"
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8"
            ),
            _prefix=prefix,
            _trailer=trailer,
        )
    return _Protocol(
        dialect=dialect,
        marker=marker,
        _trailer=f"printf '{marker}%s|%s\\n' \"$?\" \"$PWD\"",
    )


class ShellProcess:
    """一个持久 shell 进程。一个实例 = 一个「唤醒的 shell 名额」。"""

    def __init__(
        self,
        interp: Interpreter,
        cwd: str,
        shell_id: str = "",
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self.interp = interp
        self.shell_id = shell_id
        self.state = STATE_STARTING
        self.created_at = time.time()
        self.last_command = ""
        self.last_used = time.time()
        self._cwd = cwd
        self._emit = emit or (lambda _chunk: None)
        self._proto = make_protocol(interp.dialect)
        self._proc: subprocess.Popen | None = None
        self._queue: "queue.Queue[bytes | None]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._pending: list[str] = []
        self._last_emit = 0.0
        self.error = ""

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> None:
        """spawn + bootstrap 往返。失败抛 `ShellDead`（调用方转宿主 error）。"""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                list(self.interp.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 等价终端 2>&1
                cwd=self._cwd or None,
                creationflags=flags,
            )
        except OSError as exc:
            self.state = STATE_DEAD
            raise ShellDead(f"无法启动 shell：{exc}") from exc
        self._reader = threading.Thread(
            target=self._pump, name=f"ymt-shell-{self.shell_id or 'x'}", daemon=True
        )
        self._reader.start()
        self.state = STATE_READY
        boot = self._proto.start_command
        if boot:
            # bootstrap 也走完整往返：能即刻发现协议不通，并拿到初始 cwd。
            self._exchange(boot, timeout_s=15.0, cancel=None)

    def _pump(self) -> None:
        """读取线程：把 stdout 行推入队列；EOF 推 None（进程已退出）。"""
        stream = self._proc.stdout if self._proc is not None else None
        try:
            while stream is not None:
                line = stream.readline()
                if not line:
                    break
                self._queue.put(line)
        except Exception:  # noqa: BLE001 - 读线程异常等同 EOF，不能让它把进程拖死
            log.debug("shell 读取线程异常", exc_info=True)
        finally:
            self._queue.put(None)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def cwd(self) -> str:
        return self._cwd

    def close(self) -> None:
        """终止进程（幂等）。"""
        proc, self._proc = self._proc, None
        self.state = STATE_DEAD
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:  # noqa: BLE001 - 关闭路径不抛
            log.debug("shell 关闭异常", exc_info=True)
        finally:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- 执行 --------------------------------------------------------------
    def run(
        self,
        command: str,
        timeout_ms: int = 30000,
        cancel: object | None = None,
    ) -> ShellRun:
        """执行一条命令并取回输出/退出码/cwd。超时与取消都会**杀掉**本 shell。"""
        if not self.alive:
            self.state = STATE_DEAD
            raise ShellDead("shell 已退出，请重新执行以新建一个 shell。")
        self.state = STATE_BUSY
        self.last_command = command
        self.last_used = time.time()
        try:
            return self._exchange(command, timeout_s=max(1.0, timeout_ms / 1000.0), cancel=cancel)
        finally:
            if self.alive:
                self.state = STATE_READY
            self._flush_emit(force=True)

    def _proc_kind_is_ps(self) -> bool:
        """当前方言是否 ps（决定写 stdin 的编码口径）。"""
        return self.interp.dialect == DIALECT_PS

    def _exchange(
        self, command: str, timeout_s: float, cancel: object | None
    ) -> ShellRun:
        assert self._proc is not None and self._proc.stdin is not None
        started = time.monotonic()
        # 命令 + 空行：空行是语句终止符 —— 实测 PS 无空行会一直累积不执行（spec §3.1）。
        wire = self._proto.payload(command) + "\n\n"
        try:
            # 编码口径随方言：ps 的线格式**必须是 ASCII**（命令已 base64），
            # posix 则原样走 UTF-8 —— 用 ASCII 编 posix 会把中文命令变成逃逸序列（实测踩到）。
            self._proc.stdin.write(
                wire.encode("ascii") if self._proc_kind_is_ps() else wire.encode("utf-8")
            )
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            self.state = STATE_DEAD
            raise ShellDead("shell 已退出，请重新执行以新建一个 shell。") from exc

        buf: list[str] = []
        deadline = started + timeout_s
        while True:
            if _cancelled(cancel):
                self.close()
                raise ShellCancelled("命令已取消（shell 因状态不确定被关闭）。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise ShellTimeout(f"命令超过 {timeout_s:.0f}s 未结束（shell 已关闭）。")
            try:
                raw = self._queue.get(timeout=min(POLL_S, remaining))
            except queue.Empty:
                continue
            if raw is None:  # EOF：进程自己退了
                self.close()  # 回收句柄，让 alive 立刻反映事实（不留"看起来还活着"）
                raise ShellDead(
                    "shell 进程已退出（命令可能结束了它或解释器异常），请重新执行以新建。"
                )
            line = _decode(raw)
            exit_code, cwd = _parse_marker(line, self._proto.marker)
            if exit_code is not None:
                if cwd:
                    self._cwd = cwd
                return ShellRun(
                    output="".join(buf),
                    exit_code=exit_code,
                    cwd=self._cwd,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            buf.append(line)
            self._push_emit(line)

    # -- 监视流 ------------------------------------------------------------
    def _push_emit(self, line: str) -> None:
        self._pending.append(line)
        now = time.monotonic()
        if len(self._pending) >= EMIT_LINES or (now - self._last_emit) >= EMIT_INTERVAL_S:
            self._flush_emit(force=True)

    def _flush_emit(self, force: bool = False) -> None:
        if not self._pending:
            return
        if not force and len(self._pending) < EMIT_LINES:
            return
        chunk = "".join(self._pending)
        self._pending = []
        self._last_emit = time.monotonic()
        try:
            self._emit(chunk)
        except Exception:  # noqa: BLE001 - 监视流失败不影响执行
            log.debug("shell 监视流发射失败", exc_info=True)

    # -- 快照 --------------------------------------------------------------
    def info(self) -> dict:
        return {
            "id": self.shell_id,
            "kind": self.interp.kind,
            "state": self.state if self.alive else STATE_DEAD,
            "cwd": self._cwd,
            "pid": self.pid,
            "last_command": self.last_command,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }


def _parse_marker(line: str, marker: str) -> tuple[int | None, str]:
    """哨兵行 → (退出码, cwd)；非哨兵行返回 (None, "")。"""
    if not line.startswith(marker):
        return None, ""
    rest = line[len(marker):].strip()
    code_text, sep, cwd = rest.partition("|")
    try:
        code = int(code_text.strip())
    except (TypeError, ValueError):
        code = 0
    return code, (cwd.strip() if sep else "")


def _cancelled(cancel: object | None) -> bool:
    """协作式取消探测（docs 04 §2）：容忍任何形状的取消令牌，缺方法即视为未取消。"""
    if cancel is None:
        return False
    probe = getattr(cancel, "is_cancelled", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - 探测失败按未取消处理，不能因令牌异常中断命令
        return False


__all__ = [
    "DIALECT_OF",
    "STATE_BUSY",
    "STATE_DEAD",
    "STATE_READY",
    "STATE_STARTING",
    "Interpreter",
    "ShellCancelled",
    "ShellDead",
    "ShellError",
    "ShellProcess",
    "ShellRun",
    "ShellTimeout",
    "default_cwd",
    "detect",
]
