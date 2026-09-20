"""机密库（spec v0.0.2 §3）。

- `ymtdata/secrets/vault.dat`：**整库单 blob** `base64(DPAPI(熵=ENTROPY_VAULT, utf8(json)))`。
- `models.json` 只持 `vault://<prv_id>` 引用；明文密钥不落任何位置。
- fail-closed：解封失败/库损坏 → `get`→None、`status`→`error`、**拒绝覆盖写**（不静默降级明文）。
"""

from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from core.security.dpapi import ENTROPY_VAULT
from core.security.errors import SecretError, SecretUnavailable
from core.store.atomic import atomic_write_text

AuditFn = Callable[..., None]

_API_KEY_PREFIX = "api_key/"


def key_ref_for(provider_id: str) -> str:
    return f"vault://{provider_id}"


def secret_name(provider_id: str) -> str:
    return f"{_API_KEY_PREFIX}{provider_id}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ISecretStore(ABC):
    @abstractmethod
    def set(self, name: str, value: str) -> None: ...

    @abstractmethod
    def get(self, name: str) -> str | None: ...

    @abstractmethod
    def delete(self, name: str) -> None: ...

    @abstractmethod
    def status(self, name: str) -> Literal["stored", "missing", "error"]: ...

    @abstractmethod
    def names(self) -> list[str]: ...


class Vault(ISecretStore):
    def __init__(self, root: Path, box, audit: AuditFn | None = None) -> None:
        self.root = Path(root)
        self.path = self.root / "secrets" / "vault.dat"
        self.box = box
        self._audit = audit
        self._entries: dict[str, str] | None = None
        self._error: str | None = None

    # -- 载入（惰性，错误缓存） ---------------------------------------------
    def _load(self) -> dict[str, str] | None:
        if self._entries is not None:
            return self._entries
        if self._error is not None:
            return None
        if not self.path.exists():
            self._entries = {}
            return self._entries
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            blob = base64.b64decode(raw, validate=False)
            doc = json.loads(self.box.unseal(blob, entropy=ENTROPY_VAULT).decode("utf-8"))
            entries = {str(k): str(v) for k, v in dict(doc.get("entries", {})).items()}
        except SecretError as exc:
            self._error = exc.code
            self._notify("vault_unreadable", code=exc.code)
            return None
        except (OSError, ValueError, TypeError):
            self._error = "secret_decrypt_failed"
            self._notify("vault_unreadable", code=self._error)
            return None
        self._entries = entries
        return entries

    def _notify(self, action: str, **fields: Any) -> None:
        if self._audit is not None:
            try:
                self._audit(action, **fields)
            except Exception:  # noqa: BLE001 - 审计失败不得影响主流程
                pass

    # -- 接口 ---------------------------------------------------------------
    def status(self, name: str) -> Literal["stored", "missing", "error"]:
        entries = self._load()
        if entries is None:
            return "error"
        return "stored" if entries.get(name) else "missing"

    def get(self, name: str) -> str | None:
        entries = self._load()
        if entries is None:
            return None
        return entries.get(name) or None

    def names(self) -> list[str]:
        entries = self._load()
        return sorted(entries) if entries is not None else []

    def set(self, name: str, value: str) -> None:
        entries = self._load()
        if entries is None:
            raise SecretError(
                self._error or "secret_decrypt_failed", "机密库不可读，拒绝覆盖写入"
            )
        candidate = dict(entries)
        candidate[name] = value
        self._persist(candidate)
        self._entries = candidate

    def delete(self, name: str) -> None:
        entries = self._load()
        if entries is None:
            raise SecretError(self._error or "secret_decrypt_failed", "机密库不可读，拒绝改写")
        if name not in entries:
            return
        candidate = dict(entries)
        candidate.pop(name, None)
        self._persist(candidate)
        self._entries = candidate

    # -- 落盘 ---------------------------------------------------------------
    def _persist(self, entries: dict[str, str]) -> None:
        doc = {"v": 1, "entries": entries, "updated_at": _now()}
        try:
            blob = self.box.seal(
                json.dumps(doc, ensure_ascii=False).encode("utf-8"), entropy=ENTROPY_VAULT
            )
        except SecretUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SecretError("secret_write_failed", "机密库写入失败") from exc
        try:
            atomic_write_text(self.path, base64.b64encode(blob).decode("ascii"))
        except OSError as exc:
            raise SecretError("secret_write_failed", "机密库写入失败") from exc