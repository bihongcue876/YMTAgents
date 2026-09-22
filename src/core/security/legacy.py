"""旧凭据源：系统凭据管理器 —— **仅供迁移**（spec v0.0.2 §3.4）。

- 原 `core/gateway/keyring_store.py` 迁入此处：`keyring` 依赖降为迁移专用。
- `WinVaultKeyring` 以 `CRED_TYPE_GENERIC` 写凭据管理器 → 同账户任意进程可读回明文，
  故不再作为运行时存储；迁移完成后删除旧条目。
- 迁移顺序「写新 → 回读校验 → 改配置 → 删旧」：任一步失败都不会出现「两边都没有」。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.security.audit import AuditFn, safe_audit
from core.security.errors import SecretError
from core.security.vault import ISecretStore, key_ref_for, secret_name
from core.store.config_store import ConfigStore

log = logging.getLogger(__name__)

SERVICE = "ymt"


class LegacyKeyring:
    """系统凭据管理器存取（迁移源）。backend 可注入，便于测试。"""

    def __init__(self, service: str = SERVICE, backend: object | None = None) -> None:
        self.service = service
        self._backend = backend

    def _kr(self):
        if self._backend is not None:
            return self._backend
        import keyring

        return keyring

    def get_key(self, provider_id: str) -> str | None:
        try:
            return self._kr().get_password(self.service, provider_id)
        except Exception as exc:  # noqa: BLE001 - 凭据管理器异常一律去敏
            log.warning("旧凭据不可用：%s", type(exc).__name__)
            return None

    def delete_key(self, provider_id: str) -> bool:
        try:
            self._kr().delete_password(self.service, provider_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("删除旧凭据失败：%s", type(exc).__name__)
            return False


def migrate_keyring_to_vault(
    store: ConfigStore,
    secrets: ISecretStore,
    legacy: LegacyKeyring | None = None,
    *,
    audit: AuditFn | None = None,
) -> dict[str, int]:
    """把 `keyring://` 旧密钥迁入机密库；幂等，失败保留旧状态（spec §3.4）。"""
    legacy = legacy or LegacyKeyring()
    models = store.load("models")
    counts = {"migrated": 0, "skipped": 0, "failed": 0, "old_left": 0}

    targets = [
        p
        for p in models.providers
        if not p.local and not (p.key_ref or "").startswith("vault://")
    ]
    if not targets:
        return counts

    _backup_models(store, audit)

    changed = False
    for pc in targets:
        key = legacy.get_key(pc.id)
        if not key:
            counts["skipped"] += 1
            safe_audit(audit, "vault_migration_skipped", provider_id=pc.id)
            continue
        name = secret_name(pc.id)
        try:
            secrets.set(name, key)  # 1) 先写新
            if secrets.get(name) != key:  # 2) 回读校验
                raise SecretError("secret_write_failed", "回读校验不一致")
        except SecretError as exc:
            counts["failed"] += 1
            safe_audit(audit, "vault_migration_failed", provider_id=pc.id, code=exc.code)
            continue
        pc.key_ref = key_ref_for(pc.id)  # 3) 改配置（此时新旧都可用）
        changed = True
        counts["migrated"] += 1
        safe_audit(audit, "vault_migrated", provider_id=pc.id)
        if not legacy.delete_key(pc.id):  # 4) 最后删旧
            counts["old_left"] += 1
            safe_audit(audit, "vault_migration_old_entry_left", provider_id=pc.id)

    if changed:
        store.save("models", models)
    return counts


def _backup_models(store: ConfigStore, audit: AuditFn | None) -> Path | None:
    """迁移前置备份 `models.json`（只含 key_ref 引用，**不含密钥值**）。"""
    src = store.path("models")
    if not src.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest_dir = store.root / "backup" / f"vault-migration-{ts}"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "models.json"
        shutil.copy2(src, dest)
        return dest
    except OSError as exc:
        log.warning("迁移前置备份失败：%s", type(exc).__name__)
        safe_audit(audit, "vault_migration_backup_failed", code=type(exc).__name__)
        return None