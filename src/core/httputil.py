"""有界 HTTP 抓取共用层（core 内部；MCP 传输与检索模块共用）。

自 `core/mcp/transport.py` 提炼（2026-09-25，检索模块轮）：

- **禁一切重定向**（安全修订轮 F2 语义）：urllib 默认跟随重定向并把原请求头
  （含 vault 凭据头）带到重定向目标，HTTPS→HTTP 降级重定向也被跟随——等于凭据
  外泄与内网 SSRF 的通道。禁用后重定向以 HTTPError 形态浮出，交调用方错误分支。
- **限量读取**：响应体不得超过 `MAX_RESPONSE_BYTES`（恶意/失控端点不得打满内存）。

只依赖标准库。不做重试、不做 UA 伪装；调用方自行构造 `urllib.request.Request`。
"""

from __future__ import annotations

import urllib.request

#: 单次响应体的字节上限（安全修订轮同值，MCP 与 retrieval 共用）。
MAX_RESPONSE_BYTES = 8_388_608


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝一切重定向（安全修订轮 F2）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


#: 模块级共享 opener（线程安全；不携带任何逐请求状态）。
NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirect)


def open_bounded(
    req: urllib.request.Request, timeout: float, limit: int = MAX_RESPONSE_BYTES
) -> tuple[str, object]:
    """禁重定向 + 限量读取地打开响应；返回 (正文文本, 响应头对象)。

    正文按 UTF-8 解码（无法解码字节以替换符呈现——外部内容本就不可信任到字节级）。
    """
    with NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
        body = resp.read(limit).decode("utf-8", "replace")
        headers = resp.headers
    return body, headers
