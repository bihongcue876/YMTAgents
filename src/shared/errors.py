"""错误码（spec §7）。

用于 ErrorReport.code。模型可见信息一律去敏（docs 09 §5）。
"""

from enum import Enum


class ErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    PROVIDER_NOT_FOUND = "provider_not_found"
    KEY_MISSING = "key_missing"
    KEY_ERROR = "key_error"
    WHITELIST_BLOCKED = "whitelist_blocked"
    AUTH_ERROR = "auth_error"
    NETWORK_ERROR = "network_error"
    CONTEXT_OVERFLOW = "context_overflow"
    SESSION_NOT_FOUND = "session_not_found"
    STORAGE_ERROR = "storage_error"
    INTERNAL = "internal"


class ErrorScope(str, Enum):
    SESSION = "session"
    CONFIG = "config"
    GATEWAY = "gateway"
    SYSTEM = "system"


# 网关异常 -> ErrorCode 的映射口径（docs 09 §5，spec §2.2）
GATEWAY_EXCEPTION_CODE = {
    "GatewayTimeout": ErrorCode.NETWORK_ERROR,
    "GatewayBlocked": ErrorCode.WHITELIST_BLOCKED,
    "GatewayAuthError": ErrorCode.AUTH_ERROR,
    "GatewayNetworkError": ErrorCode.NETWORK_ERROR,
    "GatewayProtocolError": ErrorCode.NETWORK_ERROR,
}
