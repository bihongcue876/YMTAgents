"""`/` 命令系统（纯函数）：与按钮等价、可键盘选择，不新开任何提权/旁路通道。

设计约束（承 docs 05 §3、09 §3）：
- 命令**只是**既有 GUI 动作的键盘入口：产生与点按钮**完全相同**的信封请求或页面切换，
  不绕过权限关卡、不新增 core 通道、不改任何状态真值。
- 本模块只做**解析与匹配**（纯函数、可单测）；执行由 `MainWindow` 按 `action` 分派。
- 未知命令**不静默**：解析返回 `None` 并给出可读原因，由界面提示，不当作普通消息发送。
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIX = "/"


@dataclass(frozen=True)
class Command:
    name: str  # 不含前导 `/`
    summary: str
    usage: str = ""
    action: str = "navigate"  # navigate | new_session | compress | detail | theme | help
    target: str = ""  # navigate 的页面 key，或 theme 的取值枚举说明


#: 命令表（与既有按钮一一对应；不含任何模型/工具可见的能力）。
COMMANDS: tuple[Command, ...] = (
    Command("help", "查看可用命令", action="help"),
    Command("new", "新建对话", usage="工作区名可省", action="new_session"),
    Command("clear", "清空当前会话视图（仅界面，不改事件流）", action="clear_view"),
    Command("compress", "立即压缩会话记忆", action="compress"),
    Command("detail", "显示 / 收起会话详情右栏", action="detail"),
    Command("theme", "切换主题", usage="light|dark", action="theme", target="light|dark"),
    Command("model", "打开模型配置页", action="navigate", target="models"),
    Command("persona", "打开角色配置页", action="navigate", target="personas"),
    Command("plugins", "打开插件（MCP）页", action="navigate", target="plugins"),
    Command("skills", "打开技能页", action="navigate", target="skills"),
    Command("terminal", "打开终端页", action="navigate", target="terminal"),
    Command("thinking", "打开副思考链页", action="navigate", target="thinking"),
    Command("library", "打开小图书馆页", action="navigate", target="library"),
    Command("workspaces", "打开工作区页", action="navigate", target="workspaces"),
    Command("settings", "打开系统设置页", action="navigate", target="settings"),
)

_BY_NAME = {command.name: command for command in COMMANDS}


def by_name(name: str) -> Command | None:
    """命令名（不含 `/`）→ 命令；未知返回 None。"""
    return _BY_NAME.get(str(name or "").casefold())


def is_command(text: str) -> bool:
    """输入是否已进入命令态（以便界面显示命令面板）。"""
    return str(text or "").lstrip().startswith(PREFIX)


def match(text: str) -> list[Command]:
    """按命令名前缀过滤（大小写不敏感）；`/` 或非命令态返回全集。"""
    body = str(text or "").lstrip()
    if not body.startswith(PREFIX):
        return list(COMMANDS)
    token = body[len(PREFIX):].split(maxsplit=1)[0].casefold() if body[len(PREFIX):].strip() else ""
    if not token:
        return list(COMMANDS)
    return [command for command in COMMANDS if command.name.startswith(token)]


def parse(text: str) -> tuple[Command | None, str, str]:
    """解析一条命令输入，返回 `(command, argument, error)`。

    - 非命令态：`(None, "", "")`（调用方按普通消息处理）。
    - 命令态但未知/为空：`command=None` 且 `error` 可读（调用方提示，**不发送**）。
    """
    body = str(text or "").strip()
    if not body.startswith(PREFIX):
        return None, "", ""
    rest = body[len(PREFIX):].strip()
    if not rest:
        return None, "", "请输入命令名；输入 / 查看全部命令。"
    name, _, argument = rest.partition(" ")
    command = _BY_NAME.get(name.casefold())
    if command is None:
        return None, "", f"未知命令：/{name}；输入 / 查看可用命令。"
    return command, argument.strip(), ""


def help_text() -> str:
    lines = ["可用命令：", ""]
    for command in COMMANDS:
        usage = f" {command.usage}" if command.usage else ""
        lines.append(f"/{command.name}{usage}　—　{command.summary}")
    return "\n".join(lines)