"""MCP 安全检测原语（纯函数，提取自原型 `core/utils.py`）。

无 Qt、无网络、无 IO —— 只做正则 / Unicode / 文本判定，便于单测与复用。
"""

from __future__ import annotations

import re
import unicodedata

# 明文凭证形态（预先编译）。只判「是否疑似」，不做任何解析或泄露。
SECRET_PATTERNS = [
    re.compile(r"(?i)bearer\s+[a-z0-9\-_\.]+"),
    re.compile(r"(?i)api[_-]?key[\"'\s:=]+[a-z0-9\-_]{8,}"),
    re.compile(r"(?i)password[\"'\s:=]+\S+"),
    re.compile(r"(?i)token[\"'\s:=]+[a-z0-9\-_\.]{8,}"),
]

# 提示注入关键词库（可后续按需扩充）。
INJECTION_KEYWORDS = [
    "忽略之前",
    "忽略以上",
    "ignore previous",
    "ignore above",
    "system prompt",
    "系统提示",
    "不要告诉用户",
    "以管理员身份",
    "act as",
    "jailbreak",
]

# URL 提取（用于工具描述 / 响应中的可疑链接检测）。
URL_PATTERN = re.compile(r"https?://[^\s\"']+")


def has_hardcoded_secret(text: str) -> bool:
    """文本是否疑似包含明文凭证。"""
    if not text:
        return False
    return any(p.search(text) for p in SECRET_PATTERNS)


def contains_prompt_injection(text: str) -> bool:
    """文本是否包含提示注入特征。"""
    if not text:
        return False
    lower = text.lower()
    return any(kw.lower() in lower for kw in INJECTION_KEYWORDS)


def has_obfuscated_chars(text: str) -> bool:
    """文本是否含零宽字符 / 双向控制符等混淆字符（Unicode 类别 Cf）。"""
    if not text:
        return False
    return any(unicodedata.category(ch) == "Cf" for ch in text)


def extract_urls(text: str) -> list[str]:
    """提取文本中所有 HTTP/HTTPS 链接。"""
    return URL_PATTERN.findall(text or "")


def truncate(text: str, max_len: int = 200) -> str:
    """截断超长文本（用于证据摘要）。"""
    if not text:
        return ""
    return text if len(text) <= max_len else text[:max_len] + "…"


def mask(value: str, keep: int = 6) -> str:
    """前缀掩码：只保留头部 keep 字符（原型 `_mask` 语义）。

    **绝不回显完整密钥。**
    """
    if not value:
        return ""
    if len(value) <= keep:
        return value + "***"
    return value[:keep] + "***"


def mask_center(text: str, head: int = 8, tail: int = 4, marker: str = "***") -> str:
    """中心掩码：长文本只保留头尾片段（原型 `_mask_center` 语义）。"""
    if not text:
        return ""
    if len(text) <= head + tail:
        return text[:head] + marker
    return text[:head] + marker + text[-tail:]


def as_bool(value: object) -> bool:
    """宽松布尔判定：部分服务器以字符串 / 数字返回注解。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False