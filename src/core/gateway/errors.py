"""网关异常类型（spec §2.2）。

每个异常携带 `code`，映射到 ErrorReport.code（见 shared.errors）。
"""

from __future__ import annotations


class GatewayError(Exception):
    """网关异常基类。"""

    default_code = "internal"

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class GatewayTimeout(GatewayError):
    """静默超时（连续无增量达阈值）。"""

    default_code = "network_error"


class GatewayBlocked(GatewayError):
    """base_url 出口被白名单拦截。"""

    default_code = "whitelist_blocked"


class GatewayAuthError(GatewayError):
    """密钥缺失或供应商返回 401/403。"""

    default_code = "auth_error"


class GatewayNetworkError(GatewayError):
    """连接/网络类错误。"""

    default_code = "network_error"


class GatewayProtocolError(GatewayError):
    """供应商以协议错误拒绝（404/400/422 等）。

    默认码为 `protocol_error`——**不再默认冒充 provider_not_found**。
    归因到更具体的码（auth_error / model_not_found）由 `provider._map_exception` 显式给出（spec rev5 §3）。
    """

    default_code = "protocol_error"
