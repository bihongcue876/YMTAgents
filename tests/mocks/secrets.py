"""机密库替身（spec v0.0.2 §9）。

- `FakeBox`：可逆的假封存（前缀标记），用于在非 Windows / 无 DPAPI 环境下
  驱动真实 `Vault` 走通加解密管线；
- `NullBox`：永远不可用（模拟 crypt32 缺失）；
- `BrokenBox`：可封存、不可解封（模拟换账户 / 密文损坏）；
- `FakeVault`：内存版 `ISecretStore`，用于注入网关与自检。
"""

from __future__ import annotations

from core.security.errors import SecretError, SecretUnavailable
from core.security.vault import ISecretStore

_MAGIC = b"FB1:"


class FakeBox:
    """可逆假封存；`entropy` 参与前缀，便于断言隔离。"""

    def available(self) -> bool:
        return True

    def seal(self, data: bytes, *, entropy: bytes = b"") -> bytes:
        return _MAGIC + entropy + b"|" + data

    def unseal(self, blob: bytes, *, entropy: bytes = b"") -> bytes:
        prefix = _MAGIC + entropy + b"|"
        if not blob.startswith(prefix):
            raise SecretError("secret_decrypt_failed", "密文不属于当前账户或已损坏")
        return blob[len(prefix) :]


class NullBox:
    """本机加密不可用。"""

    def available(self) -> bool:
        return False

    def seal(self, data: bytes, *, entropy: bytes = b"") -> bytes:
        raise SecretUnavailable()

    def unseal(self, blob: bytes, *, entropy: bytes = b"") -> bytes:
        raise SecretUnavailable()


class BrokenBox:
    """封存可用但解封必然失败（换账户 / 损坏）。"""

    def available(self) -> bool:
        return True

    def seal(self, data: bytes, *, entropy: bytes = b"") -> bytes:
        return b"BROKEN" + data

    def unseal(self, blob: bytes, *, entropy: bytes = b"") -> bytes:
        raise SecretError("secret_decrypt_failed", "密文不属于当前账户或已损坏")


class FakeVault(ISecretStore):
    """内存机密库；`error` 置位模拟「库不可读」的 fail-closed 行为。"""

    def __init__(self, data: dict[str, str] | None = None, *, error: str | None = None) -> None:
        self.data: dict[str, str] = dict(data or {})
        self.error = error
        self.calls: list[tuple] = []

    def set(self, name: str, value: str) -> None:
        self.calls.append(("set", name))
        if self.error:
            raise SecretError(self.error)
        self.data[name] = value

    def get(self, name: str) -> str | None:
        self.calls.append(("get", name))
        if self.error:
            return None
        return self.data.get(name)

    def delete(self, name: str) -> None:
        self.calls.append(("delete", name))
        if self.error:
            raise SecretError(self.error)
        self.data.pop(name, None)

    def status(self, name: str) -> str:
        self.calls.append(("status", name))
        if self.error:
            return "error"
        return "stored" if name in self.data else "missing"

    def names(self) -> list[str]:
        if self.error:
            return []
        return sorted(self.data)