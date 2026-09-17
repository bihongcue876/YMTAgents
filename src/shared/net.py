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


def is_secure_transport(url: str) -> bool:
    """传输保密性判定（rev15）：

    - `https://` —— 唯一合法的远程传输方式（凭据与对话内容都走 TLS）；
    - `http://` —— **仅本机回环**允许（本地模型服务没有 TLS，但流量不出机器）；
    - 其余 scheme（file:/ftp:/无 scheme）一律不安全。
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        return True
    if scheme == "http":
        return domain_of(url) in LOCAL_HOSTS
    return False
