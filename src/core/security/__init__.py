"""机密存储（v0.0.2）：DPAPI 封存、机密库、旧密钥迁移。

- `dpapi.py`：Windows DPAPI（ctypes，免口令、CurrentUser 作用域）。
- `vault.py`：`ymtdata/secrets/vault.dat` 机密库（API Key 等）。
- `legacy.py`：系统凭据管理器读取与一次性迁移（keyring → vault）。
"""

from core.security.dpapi import ENTROPY_VAULT, DpapiBox
from core.security.errors import SecretError, SecretUnavailable
from core.security.vault import ISecretStore, Vault, key_ref_for, secret_name

__all__ = [
    "ENTROPY_VAULT",
    "DpapiBox",
    "SecretError",
    "SecretUnavailable",
    "ISecretStore",
    "Vault",
    "key_ref_for",
    "secret_name",
]