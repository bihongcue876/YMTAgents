"""密钥存取（docs 03 §3.1 / 09 §7）。

- 密钥只存系统凭据管理器（keyring），配置文件仅持 key_ref 引用。
- 密钥永不进入 args / 提示词 / 事件流 / 日志 / 错误信息。
- backend 可注入，便于测试（不触碰真实凭据管理器）。
"""

from __future__ import annotations

import logging
from typing import Literal

log = logging.getLogger(__name__)

SERVICE = "ymt"


class KeyringStore:
    def __init__(self, service: str = SERVICE, backend: object | None = None) -> None:
        self.service = service
        self._backend = backend

    def _kr(self):
        if self._backend is not None:
            return self._backend
        import keyring

        return keyring

    def set_key(self, provider_id: str, key: str) -> None:
        self._kr().set_password(self.service, provider_id, key)

    def get_key(self, provider_id: str) -> str | None:
        try:
            return self._kr().get_password(self.service, provider_id)
        except Exception as exc:  # noqa: BLE001 - 凭据管理器异常一律去敏
            log.warning("凭据不可用：%s", type(exc).__name__)
            return None

    def delete_key(self, provider_id: str) -> None:
        try:
            self._kr().delete_password(self.service, provider_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除凭据失败：%s", type(exc).__name__)

    def status(self, provider_id: str) -> Literal["stored", "missing", "error"]:
        try:
            return "stored" if self._kr().get_password(self.service, provider_id) else "missing"
        except Exception:  # noqa: BLE001
            return "error"
