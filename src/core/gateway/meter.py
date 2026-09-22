"""用量计量（docs 03 §3.1 覆盖链 / spec §2.2）。

网关是计量收口处：一切模型调用（含 btcm/dpim 内部调用）的 usage 在此累计。
"""

from __future__ import annotations

from shared.envelope import Usage


class Meter:
    def __init__(self) -> None:
        self.total = Usage()

    def add(self, usage: Usage | None) -> Usage:
        if usage is None:
            return self.total
        self.total.prompt_tokens += usage.prompt_tokens
        self.total.completion_tokens += usage.completion_tokens
        self.total.total_tokens += usage.total_tokens
        return self.total
