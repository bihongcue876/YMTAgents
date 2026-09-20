"""机密相关异常（spec v0.0.2 §6）。

`code` 与 `shared.errors.ErrorCode` 同值，供网关/控制器按真实原因归码。
"""

from __future__ import annotations


class SecretError(RuntimeError):
    """机密存取失败；`code` 为 `shared.errors` 中的码名。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class SecretUnavailable(SecretError):
    """本机加密不可用（非 Windows / crypt32 装载或调用失败）。"""

    def __init__(self, message: str = "本机加密不可用") -> None:
        super().__init__("secret_unavailable", message)