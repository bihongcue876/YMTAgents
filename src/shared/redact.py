"""敏感信息脱敏（docs 09 §5）。

铁律：密钥**永不**进入 args / 提示词 / 事件流 / 日志 / 错误信息（docs 09 B3）。
本模块是这条铁律的**最后一道防线**：任何将要落盘或回显的文本都先过 `redact()`，
即便上游某处不慎带出密钥，也不会被持久化、也不会展示给用户。

实测（rev9 §3）：当前各路径均未泄露密钥；本层是**防御性**的，不是补救。
"""

from __future__ import annotations

import re

MASK = "«已脱敏»"

#: 已知形态的密钥模式 → 掩码。宁可多掩（极少数含 `sk-` 的正常文本会被打码），
#: 也不放过一个真密钥。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI / DeepSeek / Moonshot 等：sk-xxx、pk_xxx、rk-xxx；Slack：xoxb-xxx
    (re.compile(r"\b(?:sk|pk|rk|xox[baprs])[-_][A-Za-z0-9_\-]{4,}"), MASK),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 " + MASK),
    # key=value / "api_key": "value" / api-key: value（password 族：安全修订轮 F9 补全）
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?token|secret|password|passwd|passphrase)\b"
            r"\s*[\"']?\s*[:=]\s*[\"']?([^\s\"',}]{4,})"
        ),
        r"\1=" + MASK,
    ),
    # Google API key（安全修订轮 F9）
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), MASK),
    # Groq / HuggingFace 形态（安全修订轮 F9）
    (re.compile(r"\b(?:gsk|hf)_[A-Za-z0-9]{20,}"), MASK),
    # 裸 JWT（header.payload[.signature]，安全修订轮 F9）
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]{10,})?"),
        MASK,
    ),
    # Authorization: Basic <b64>（Bearer 已有；安全修订轮 F9 补 Basic）
    (re.compile(r"(?i)\b(authorization)\s*:\s*basic\s+[A-Za-z0-9+/=]{8,}"), r"\1: " + MASK),
)


def redact(text: str | None) -> str | None:
    """把文本中的密钥形态替换为掩码；`None` 原样返回（便于直接用于可选字段）。"""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out
