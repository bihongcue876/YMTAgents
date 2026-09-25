r"""高危命令判定（docs 09 §2 restricted 档的最小落实）。

性质：**静态清单 + 归类判定**。清单在源码中公开可读、可测试、不联网、不混淆
（防后门自查：无隐藏通道、无远程查询、无行为差异）。

判据设计原则：**宁缺毋滥**——清单只覆盖「不可逆 / 系统级 / 经典后门路径」：
- `rm -rf ./build` 这类正常开发操作**不**命中；`rm -rf /`、`Remove-Item -Recurse -Force C:\` 才命中；
- 语义含糊的命令名（`format` / `shutdown` / `reboot` / `halt`）只在**命令位置**命中，
  避免 `git log --format=%H`、`npm run format` 被误判；
- `rm` 的「递归 + 强制 + 危险目标」三条件用**逐个 token 判定**而非单条正则：
  正则难以稳当覆盖 `rm -rf /`／`rm -r -f ~`／`rm -fr $HOME` 这些等价写法，
  而漏判的代价远大于多判。

命中后由 `ShellManager` 决定处置：`allow_restricted` 为假（默认）→ 策略直接拒绝并留痕；
为真（须改配置文件显式启用）→ 仍走 confirm 关卡，卡片标注「高危」。
"""

from __future__ import annotations

import re

#: 命令位置（行首，或 `;`/`&&`/`||`/`|` 分隔后的首词）。归一化后分隔符后恰有一个空格。
_CMD = r"(?:^|[;&|]+ )"

#: 归类名 → 正则（匹配前会做小写化 + 空白归一，故正则里一律用单空格）。
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ---- 磁盘与分区：误操作即不可逆 ----
    ("磁盘/分区操作", re.compile(_CMD + r"format(?:\.com)?(?:\s|$)")),
    ("磁盘/分区操作", re.compile(r"\b(?:diskpart|mkfs(?:\.\w+)?|fdisk|parted)\b")),
    ("磁盘/分区操作", re.compile(r"\bdd\s+(?:[^|;&]*\s)?if=")),
    ("磁盘/分区操作", re.compile(r"\b(?:format-volume|clear-disk|remove-partition)\b")),
    # ---- 递归删除 Windows 盘根 / PowerShell 强删根与家目录 ----
    ("递归删除根或家目录", re.compile(r"\b(?:rd|rmdir)\s+/s\s+/q\s+[a-z]:[\\/]?(?:\s|$)")),
    ("递归删除根或家目录", re.compile(r"\bdel\s+/f\s+/s\s+/q\s+[a-z]:[\\/]?(?:\s|$)")),
    (
        "递归删除根或家目录",
        re.compile(
            r"\bremove-item\b(?=[^|;&]*-recurse)(?=[^|;&]*-force)"
            r"[^|;&]*?\s(?:[a-z]:[\\/]?(?:\s|$)|~(?:\s|$)|\$home(?:\s|$)|"
            r"\$env:userprofile(?:\s|$))"
        ),
    ),
    # ---- 关机 / 重启 / 停机 ----
    ("关机或重启", re.compile(_CMD + r"(?:shutdown|reboot|poweroff|halt)(?:\s|$)")),
    ("关机或重启", re.compile(r"\b(?:restart-computer|stop-computer)\b")),
    ("关机或重启", re.compile(_CMD + r"init\s+0(?:\s|$)")),
    # ---- 账户与系统策略 ----
    ("账户或系统策略修改", re.compile(r"\bnet\s+user\b")),
    ("账户或系统策略修改", re.compile(r"\b(?:icacls|takeown|cacls)\b")),
    ("账户或系统策略修改", re.compile(r"\breg\s+delete\b")),
    ("账户或系统策略修改", re.compile(r"\bset-executionpolicy\b")),
    # ---- 强制杀进程 ----
    ("强制结束进程", re.compile(r"\btaskkill\b[^|;&]*\s/f(?:\s|$)")),
    ("强制结束进程", re.compile(r"\bstop-process\b[^|;&]*\s-force(?:\s|$)")),
    ("强制结束进程", re.compile(r"\bkill\s+-9\s+1(?:\s|$)")),
    # ---- 下载即执行（经典后门路径；docs 09 §4 B1 的注入面）----
    (
        "下载内容直接执行",
        re.compile(
            r"\b(?:curl|wget|iwr|invoke-webrequest)\b[^|;&]*\|\s*"
            r"(?:bash|sh|zsh|iex|invoke-expression)\b"
        ),
    ),
)

#: 危险删除目标：根、根下全部、家目录（含「目录名/」形态，安全修订轮 F6）。
_DANGEROUS_TARGET = re.compile(
    r"^(?:/|/\*|~|~/|~/\*|\$home|\$home/|\$home/\*|\$\{home\}|\$\{home\}/)$"
)

#: 可跳过的命令前缀：启动器与进程外包装（安全修订轮 F6）——
#: `sudo rm -rf /`、`env X=1 rm -rf /`、`find … | xargs rm -rf /` 同危。
_LAUNCHERS = frozenset({"sudo", "doas", "nohup", "env", "nice", "xargs"})


def _unquote(token: str) -> str:
    """剥掉首尾成对引号（bash/PS 都允许把命令名或目标放进引号，安全修订轮 F6）。"""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'`":
        return token[1:-1]
    return token


def _rm_removes_root(text: str) -> bool:
    """逐 token 判定「rm + 递归 + 强制 + 危险目标」。

    覆盖所有等价写法（`-rf` / `-fr` / `-r -f` / `-f -r`），只认真正危险的目标；
    引号包裹（`"rm" -rf /`、`rm -rf "~"`）先去引号再判；rm 不在段首但前面只是
    启动器 / 环境赋值前缀（`sudo`、`env X=1`、`xargs` 管尾）同样命中（安全修订轮 F6）。
    """
    for segment in re.split(r"[;&|]+", text):
        tokens = [_unquote(tok) for tok in segment.split()]
        index = 0
        while index < len(tokens):
            tok = tokens[index]
            if tok in _LAUNCHERS:
                index += 1
                continue
            if "=" in tok and index < len(tokens) - 1:
                index += 1  # env 风格赋值前缀（如 env EDITOR=vim rm …）：继续找命令位
                continue
            break
        if index >= len(tokens) or tokens[index] != "rm":
            continue
        args = tokens[index + 1:]
        flags = "".join(a[1:] for a in args if a.startswith("-") and len(a) > 1)
        if "r" not in flags or "f" not in flags:
            continue
        if any(_DANGEROUS_TARGET.match(a) for a in args if not a.startswith("-")):
            return True
    return False


def classify(command: str) -> str | None:
    """判断命令是否命中高危清单；命中返回归类名，否则 `None`。

    归一化：小写 + 行内空白折叠成单空格 —— **换行是语句分隔符，逐行判**
    （旧行为把换行折叠成空格，`echo hi\nrm -rf ~` 里的第二条命令会脱离命令位漏判，
    安全修订轮 F6）。纯函数，便于数据驱动测试。
    """
    for line in (command or "").lower().splitlines():
        text = " ".join(line.split())
        if not text:
            continue
        if _rm_removes_root(text):
            return "递归删除根或家目录"
        for label, pattern in RULES:
            if pattern.search(text):
                return label
    return None


def rule_count() -> int:
    """清单条数（供测试断言清单非空、未被误删）。"""
    return len(RULES) + 1  # +1 = rm 的 token 判定规则
