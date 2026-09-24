"""小图书馆外部对话的 `^` 指令解析；不调用 shell、不执行任意代码。"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

COMMANDS = {
    "help", "data", "interaction", "source", "cmdmsg", "update", "compress", "delete", "node", "merge"
}
HELP_TEXT = (
    "^help · ^data <资料> · ^interaction <对话> · ^source <来源> · ^cmdmsg <指令文本>\n"
    "^update（修复来源索引）· ^compress（合并重复图节点）· ^delete <event_id>（软删除来源）\n"
    "^node add <标题> | <内容> · ^node delete <node_id> · ^merge <保留node_id> <合并node_id>"
)
_COMMAND = re.compile(r"^\s*\^([a-z][a-z0-9_-]*)(?:\s+([\s\S]*))?\s*$", re.I)


@dataclass(frozen=True)
class DpimCommand:
    name: str
    arguments: str


def parse_command(text: str) -> DpimCommand | None:
    match = _COMMAND.match(str(text or ""))
    if not match:
        return None
    return DpimCommand(match.group(1).casefold(), (match.group(2) or "").strip())


def split_words(value: str) -> list[str]:
    try:
        return shlex.split(value, posix=False)
    except ValueError as exc:
        raise ValueError("指令引号不成对。") from exc


def parse_node_add(value: str) -> tuple[str, str]:
    title_part, separator, content = value.partition("|")
    if not separator:
        raise ValueError("用法：^node add <标题> | <内容>")
    title_tokens = split_words(title_part.strip())
    title = " ".join(token.strip("\"'") for token in title_tokens).strip()
    body = content.strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in {"'", '"'}:
        body = body[1:-1]
    if not title or not body:
        raise ValueError("节点标题与内容均不能为空。")
    if len(title) > 60 or len(body) > 20_000:
        raise ValueError("节点标题最多 60 字，内容最多 20000 字。")
    return title, body
