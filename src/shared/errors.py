"""错误码（spec §7 / rev5）。

用于 ErrorReport.code。模型可见信息一律去敏（docs 09 §5）。

码的划分依据**真实原因**，一个码只承担一种语义（spec rev5 §2）：
- `provider_not_found` 只表示「供应商 id 不存在」，不得挪作他用；
- 未绑定模型是**无引用**，不是「找不到」，用 `model_unbound`；
- 模型不属于任何供应商、或被供应商拒绝，用 `model_not_found`；
- 其余协议异常用 `protocol_error` 兜底。

`ERROR_TEXT` 是「码 → 前端中文提示」的单一来源，供前端展示与后端默认文案使用。
"""

from enum import Enum


class ErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    PROVIDER_NOT_FOUND = "provider_not_found"  # 供应商 id 不存在（本义，勿挪作他用）
    MODEL_UNBOUND = "model_unbound"  # 未绑定任何模型：会话级与全局槽位皆空（无引用）
    MODEL_NOT_FOUND = "model_not_found"  # 模型不属于任何已配置供应商，或被供应商拒绝
    PROTOCOL_ERROR = "protocol_error"  # 供应商协议错误，无法归入以上
    KEY_MISSING = "key_missing"
    KEY_ERROR = "key_error"
    SECRET_UNAVAILABLE = "secret_unavailable"  # 本机加密不可用（v0.0.2）
    SECRET_DECRYPT_FAILED = "secret_decrypt_failed"  # 密文不可解（v0.0.2）
    SECRET_WRITE_FAILED = "secret_write_failed"  # 机密库写入失败（v0.0.2）
    WHITELIST_BLOCKED = "whitelist_blocked"
    INSECURE_TRANSPORT = "insecure_transport"  # 明文传输：非本机地址未使用 https（rev15）
    AUTH_ERROR = "auth_error"
    NETWORK_ERROR = "network_error"
    CONTEXT_OVERFLOW = "context_overflow"
    SESSION_NOT_FOUND = "session_not_found"
    STORAGE_ERROR = "storage_error"
    INTERNAL = "internal"
    # v0.0.3：工具调用六码（docs 09 §3）；一码一义，按真实原因归码
    TOOL_DENIED = "tool_denied"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_CANCELLED = "tool_cancelled"
    TOOL_UNAVAILABLE = "tool_unavailable"
    TOOL_INVALID_ARGS = "tool_invalid_args"
    TOOL_BACKEND_ERROR = "tool_backend_error"
    # v0.0.6：工作区策略拒绝（禁设位置、默认工作区不可动）。
    # 为什么不复用既有码：`tool_denied` 是**工具管线**的码（docs 09 §5），
    # `whitelist_blocked` 是**网络出口**的码 —— 两者语义都不是「工作区位置不许」。
    # 按真实原因新开一码，胜过把「格式不合法」的 `invalid_request` 挪作他用（一码一义）。
    WORKSPACE_DENIED = "workspace_denied"
    # DPIM 库 root 命中宿主禁设清单或重复登记。
    LIBRARY_DENIED = "library_denied"


# 码 → 前端中文提示（单一来源）。具体场景可在 message 中补充对象与操作指引。
ERROR_TEXT: dict[str, str] = {
    ErrorCode.INVALID_REQUEST.value: "请求格式不合法",
    ErrorCode.PROVIDER_NOT_FOUND.value: "供应商不存在",
    ErrorCode.MODEL_UNBOUND.value: "尚未绑定模型",
    ErrorCode.MODEL_NOT_FOUND.value: "该模型在供应商不可用",
    ErrorCode.PROTOCOL_ERROR.value: "供应商拒绝了请求",
    ErrorCode.KEY_MISSING.value: "凭据不可用",
    ErrorCode.KEY_ERROR.value: "加密库不可用",
    ErrorCode.SECRET_UNAVAILABLE.value: "本机加密不可用，无法安全保存密钥",
    ErrorCode.SECRET_DECRYPT_FAILED.value: "密钥不可解（密文不属于当前账户或已损坏）",
    ErrorCode.SECRET_WRITE_FAILED.value: "密钥保存失败",
    ErrorCode.WHITELIST_BLOCKED.value: "已被网络白名单拦截",
    ErrorCode.INSECURE_TRANSPORT.value: "明文传输不安全：非本机地址必须使用 https://",
    ErrorCode.AUTH_ERROR.value: "凭据不可用",
    ErrorCode.NETWORK_ERROR.value: "网络或连接错误",
    ErrorCode.CONTEXT_OVERFLOW.value: "上下文超出模型窗口",
    ErrorCode.SESSION_NOT_FOUND.value: "会话不存在",
    ErrorCode.STORAGE_ERROR.value: "数据读写异常",
    ErrorCode.INTERNAL.value: "内部错误",
    ErrorCode.TOOL_DENIED.value: "工具调用被拒绝（权限不足或用户拒绝）",
    ErrorCode.TOOL_TIMEOUT.value: "工具调用超时",
    ErrorCode.TOOL_CANCELLED.value: "工具调用被取消",
    ErrorCode.TOOL_UNAVAILABLE.value: "工具不可用（未注册或服务器未就绪）",
    ErrorCode.TOOL_INVALID_ARGS.value: "工具参数无效",
    ErrorCode.TOOL_BACKEND_ERROR.value: "工具后端错误",
    ErrorCode.WORKSPACE_DENIED.value: "该位置不可作为工作区",
    ErrorCode.LIBRARY_DENIED.value: "该位置不可作为书库目录",
}


def error_text(code: str) -> str:
    """码 → 中文提示；未知码回退通用文案（不泄露码以外的内部信息）。"""
    return ERROR_TEXT.get(code, "未知错误")


# 网关异常 -> ErrorCode 的映射口径（docs 09 §5，spec §2.2）。
# 注意：异常自带 code 时以自带为准（loop._code_of 优先取 exc.code），本表是兜底。
GATEWAY_EXCEPTION_CODE = {
    "GatewayTimeout": ErrorCode.NETWORK_ERROR,
    "GatewayBlocked": ErrorCode.WHITELIST_BLOCKED,
    "GatewayAuthError": ErrorCode.AUTH_ERROR,
    "GatewayNetworkError": ErrorCode.NETWORK_ERROR,
    "GatewayProtocolError": ErrorCode.PROTOCOL_ERROR,
}
