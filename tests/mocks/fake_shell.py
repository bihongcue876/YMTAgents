"""假 shell：按**线格式**回话的子进程，供 shell 单测使用（不依赖真解释器）。

为什么要它：`ShellProcess` 的价值全在协议与边界（哨兵定界、退出码、cwd、超时、取消、进程退出），
用真 PowerShell 测这些会让单测变慢、跨平台失败、且难构造极端情形。假 shell 是一个纯 Python
子进程，**按同一线格式**应答，于是两个方言（ps 的 ASCII 包装 / posix 的行尾命令）都能被测到。

支持的「命令」（取首行首个词）：
    echo <文本>   回显
    utf8          回显中文（编码路径）
    ansi          回显带 SGR 颜色的行
    multi          回显三行
    emit <n>       回显 n 行（监视流合帧用）
    fail           回显一行错误并返回退出码 1
    exit <n>       返回退出码 n
    cd <dir>       切换工作目录（验证状态持续）
    pwd            回显工作目录
    sleep <秒>     睡眠（超时/取消用）
    crash          直接结束进程（验证 ShellDead）
"""

from __future__ import annotations

import base64
import os
import re
import sys
import time

MARKER_RE = re.compile(r"(__YMT_[0-9a-f]+__)")

#: 协议自带的行（前缀 / 尾行）不算用户命令。
_PROTOCOL_LINE = re.compile(r"^\$__ymt_|__YMT_[0-9a-f]+__")


def _emit(text: str, marker: str, code: int = 0) -> None:
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.write(f"{marker}{code}|{os.getcwd()}\n")
    sys.stdout.flush()


def _body_of(script: str) -> str:
    return "\n".join(
        ln for ln in script.split("\n") if ln.strip() and not _PROTOCOL_LINE.search(ln)
    ).strip()


def _handle(script: str, marker: str) -> None:
    body = _body_of(script)
    first = body.split("\n", 1)[0].strip()
    verb, _, rest = first.partition(" ")
    rest = rest.strip().strip('"').strip("'")

    if verb == "echo":
        _emit(rest, marker)
    elif verb == "utf8":
        _emit("中文回显 · 路径 D:\\示例\\中文", marker)
    elif verb == "ansi":
        _emit("\x1b[31mred\x1b[0m \x1b[1mbold\x1b[0m", marker)
    elif verb == "multi":
        _emit("line-1\nline-2\nline-3", marker)
    elif verb == "emit":
        try:
            count = int(rest or "1")
        except ValueError:
            count = 1
        _emit("\n".join(f"row-{i}" for i in range(count)), marker)
    elif verb == "fail":
        _emit("boom: command failed", marker, code=1)
    elif verb == "exit":
        try:
            code = int(rest or "0")
        except ValueError:
            code = 0
        _emit("", marker, code=code)
    elif verb == "cd":
        try:
            os.chdir(rest)
        except OSError:
            _emit("cd failed", marker, code=1)
            return
        _emit("", marker)
    elif verb == "pwd":
        _emit(os.getcwd(), marker)
    elif verb == "sleep":
        try:
            seconds = float(rest or "1")
        except ValueError:
            seconds = 1.0
        time.sleep(seconds)
        _emit("slept", marker)
    elif verb == "crash":
        sys.exit(7)
    else:
        _emit("unknown", marker)


def main() -> None:
    # 管道下的默认编码可能是系统码页；固定 UTF-8，与 ShellProcess 的解码口径一致。
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    buffered: list[str] = []
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if line.startswith("$__ymt_c=[Text.Encoding]"):
            encoded = line.split("FromBase64String('", 1)[1].split("')", 1)[0]
            script = base64.b64decode(encoded).decode("utf-8")
            marker = MARKER_RE.search(script)
            if marker:
                _handle(script, marker.group(1))
            continue
        if _PROTOCOL_LINE.search(line):
            buffered.append(line)
            marker = MARKER_RE.search(line)
            if marker:
                _handle("\n".join(buffered), marker.group(1))
                # 必须清空：否则下一条命令会把上一条的正文一起算进去（单测实测踩到）
                buffered = []
            continue
        if line.strip():
            buffered.append(line)


if __name__ == "__main__":
    main()
