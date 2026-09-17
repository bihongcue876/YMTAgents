"""网络出口白名单（docs 09 §7）。

- 规则：域名精确或 `*.后缀` 匹配。
- 默认拒绝（P2）。
- 约束本应用进程的一切出网；MCP 子进程不计入（界面另行明示）。
- `domain_of` 已下沉到 `shared.net`（gui 也要用，而 gui 不得 import core），此处保留再导出。
"""

from __future__ import annotations

from shared.net import domain_of

__all__ = ["Whitelist", "domain_of"]


def _rule_matches(rule: str, host: str) -> bool:
    rule = rule.strip().lower()
    if not rule or not host:
        return False
    if rule.startswith("*."):
        suffix = rule[2:]
        return host == suffix or host.endswith("." + suffix)
    return host == rule


class Whitelist:
    def __init__(self, rules: list[str] | None = None) -> None:
        self.rules: list[str] = list(rules or [])

    def is_allowed(self, url: str) -> bool:
        host = domain_of(url)
        return any(_rule_matches(rule, host) for rule in self.rules)

    def add(self, rule: str) -> bool:
        """加入规则；已存在返回 False。"""
        rule = rule.strip().lower()
        if not rule or rule in self.rules:
            return False
        self.rules.append(rule)
        return True

    def remove(self, rule: str) -> bool:
        rule = rule.strip().lower()
        if rule in self.rules:
            self.rules.remove(rule)
            return True
        return False
