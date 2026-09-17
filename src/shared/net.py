"""URL / 主机名工具（shared 层，core 与 gui 共用）。

放这里是因为 **gui 不得 import core**（依赖方向铁律），
而「这个地址是不是本机」两边都要判断：core 用来放宽密钥要求，gui 用来切接入类型。
"""

from __future__ import annotations

from urllib.parse import urlparse

#: 视为「本机」的主机名。本地模型服务（Ollama / LM Studio / vLLM / Xinference）
#: 默认监听在这些主机上，且**不需要 API Key**。
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})


def domain_of(url: str) -> str:
    """从 URL 提取小写主机名；无法解析时返回空串。"""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


def is_local_url(url: str) -> bool:
    """该地址是否指向本机 —— 据此启用「本地模型服务」语义（免密钥）。"""
    return domain_of(url) in LOCAL_HOSTS
