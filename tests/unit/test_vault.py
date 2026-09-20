"""core.security 单元测试：DPAPI 封装、机密库、旧密钥迁移（spec v0.0.2 §2/§3/§9）。

（脱敏层与传输保密性的既有用例仍在 `test_security.py`。）
"""

from __future__ import annotations

import base64
import json
import sys

import pytest

from core.security.dpapi import ENTROPY_VAULT, DpapiBox
from core.security.errors import SecretError, SecretUnavailable
from core.security.legacy import LegacyKeyring, migrate_keyring_to_vault
from core.security.vault import Vault, key_ref_for, secret_name
from core.store.config_store import ConfigStore
from shared.schema import ModelConfig, ModelsConfig, ProviderConfig
from tests.mocks.secrets import BrokenBox, FakeBox, FakeVault, NullBox


# ---------------------------------------------------------------------------
# DPAPI 封装（真机）
# ---------------------------------------------------------------------------
win_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")


@win_only
def test_dpapi_roundtrip_and_entropy_isolation():
    box = DpapiBox()
    assert box.available() is True
    blob = box.seal(b"secret-value", entropy=ENTROPY_VAULT)
    assert b"secret-value" not in blob
    assert box.unseal(blob, entropy=ENTROPY_VAULT) == b"secret-value"

    with pytest.raises(SecretError) as ei:  # 不同熵 → 不可解
        box.unseal(blob, entropy=b"other")
    assert ei.value.code == "secret_decrypt_failed"


@win_only
def test_dpapi_unseal_garbage_raises():
    with pytest.raises(SecretError) as ei:
        DpapiBox().unseal(b"not-a-dpapi-blob")
    assert ei.value.code == "secret_decrypt_failed"


# ---------------------------------------------------------------------------
# 机密库
# ---------------------------------------------------------------------------
def make_vault(tmp_path, box=None, audit=None) -> Vault:
    return Vault(tmp_path, box or FakeBox(), audit=audit)


def test_vault_set_get_status_names_delete(tmp_path):
    vault = make_vault(tmp_path)
    name = secret_name("prv_1")
    assert vault.status(name) == "missing"
    assert vault.get(name) is None

    vault.set(name, "sk-abc")
    assert vault.status(name) == "stored"
    assert vault.get(name) == "sk-abc"
    assert vault.names() == [name]

    vault.delete(name)
    assert vault.status(name) == "missing"
    assert vault.names() == []
    vault.delete(name)  # 幂等，不抛


def test_vault_persists_without_plaintext(tmp_path):
    vault = make_vault(tmp_path)
    vault.set(secret_name("prv_1"), "sk-very-secret")

    raw = vault.path.read_text(encoding="utf-8")
    assert "sk-very-secret" not in raw
    # 是 base64 封存，不是明文 JSON（「密文不含明文」由 DPAPI 用例保证）
    assert not raw.strip().startswith("{")

    fresh = make_vault(tmp_path)  # 新实例（重载）仍可读
    assert fresh.get(secret_name("prv_1")) == "sk-very-secret"


def test_vault_second_set_keeps_previous_entries(tmp_path):
    vault = make_vault(tmp_path)
    vault.set(secret_name("prv_1"), "a")
    vault.set(secret_name("prv_2"), "b")
    assert vault.names() == [secret_name("prv_1"), secret_name("prv_2")]
    assert vault.get(secret_name("prv_1")) == "a"


def test_vault_uses_entropy_single_source(tmp_path):
    """封存必须带 ENTROPY_VAULT —— 否则与其他用途的密文可互换。"""
    vault = make_vault(tmp_path)
    vault.set(secret_name("prv_1"), "sk-abc")
    decoded = FakeBox().unseal(
        base64.b64decode(vault.path.read_text(encoding="utf-8"), validate=False),
        entropy=ENTROPY_VAULT,
    )
    assert b"sk-abc" in decoded  # 只有正确的熵才能解出（错误熵在 FakeBox 下会抛）


def test_vault_corrupt_file_is_fail_closed(tmp_path):
    vault = make_vault(tmp_path)
    vault.set(secret_name("prv_1"), "sk-abc")
    vault.path.write_text("这不是合法的封存数据", encoding="utf-8")

    broken = make_vault(tmp_path)
    assert broken.status(secret_name("prv_1")) == "error"
    assert broken.get(secret_name("prv_1")) is None
    assert broken.names() == []

    with pytest.raises(SecretError):  # 拒绝覆盖写（不静默丢密钥）
        broken.set(secret_name("prv_1"), "sk-new")
    with pytest.raises(SecretError):
        broken.delete(secret_name("prv_1"))
    assert vault.path.read_text(encoding="utf-8") == "这不是合法的封存数据"


def test_vault_wrong_account_is_error(tmp_path):
    vault = make_vault(tmp_path)
    vault.set(secret_name("prv_1"), "sk-abc")

    other = Vault(tmp_path, BrokenBox())  # 模拟换账户：封存/解封不匹配
    assert other.status(secret_name("prv_1")) == "error"
    assert other.get(secret_name("prv_1")) is None


def test_vault_unavailable_box_never_writes_plaintext(tmp_path):
    vault = make_vault(tmp_path, box=NullBox())
    with pytest.raises(SecretUnavailable):
        vault.set(secret_name("prv_1"), "sk-abc")
    assert not vault.path.exists()


def test_vault_audits_unreadable_without_secret(tmp_path):
    seen: list[tuple] = []
    vault = make_vault(tmp_path, audit=lambda action, **f: seen.append((action, f)))
    vault.set(secret_name("prv_1"), "sk-abc")
    vault.path.write_text("坏", encoding="utf-8")

    make_vault(tmp_path, audit=lambda action, **f: seen.append((action, f))).get(
        secret_name("prv_1")
    )
    actions = [a for a, _ in seen]
    assert "vault_unreadable" in actions
    assert all("sk-abc" not in json.dumps(f) for _, f in seen)


def test_vault_audit_failure_is_swallowed(tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("audit down")

    vault = Vault(tmp_path, FakeBox(), audit=boom)
    vault.set(secret_name("prv_1"), "sk-abc")  # 审计炸了也不影响写入
    assert vault.get(secret_name("prv_1")) == "sk-abc"


def test_key_ref_and_secret_name_shapes():
    assert key_ref_for("prv_9") == "vault://prv_9"
    assert secret_name("prv_9") == "api_key/prv_9"


# ---------------------------------------------------------------------------
# 旧密钥迁移
# ---------------------------------------------------------------------------
class FakeLegacyBackend:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})
        self.deleted: list[str] = []
        self.delete_ok = True

    def get_password(self, service: str, user: str):
        assert service == "ymt"
        return self.data.get(user)

    def delete_password(self, service: str, user: str) -> None:
        if not self.delete_ok:
            raise RuntimeError("credential manager down")
        self.deleted.append(user)
        self.data.pop(user, None)


def seed_models(tmp_path, *, key_ref="keyring://ymt/prv_1", local=False) -> ConfigStore:
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    models = ModelsConfig()
    models.providers.append(
        ProviderConfig(
            id="prv_1",
            name="P",
            base_url="https://api.test.com/v1",
            key_ref=key_ref,
            local=local,
            models=[ModelConfig(id="m1", ctx_window=1000)],
        )
    )
    store.save("models", models)
    return store


def test_migration_moves_key_and_updates_ref(tmp_path):
    store = seed_models(tmp_path)
    vault = make_vault(tmp_path)
    legacy = LegacyKeyring(backend=FakeLegacyBackend({"prv_1": "sk-old"}))

    counts = migrate_keyring_to_vault(store, vault, legacy)
    assert counts == {"migrated": 1, "skipped": 0, "failed": 0, "old_left": 0}
    assert vault.get(secret_name("prv_1")) == "sk-old"
    assert store.load("models").providers[0].key_ref == "vault://prv_1"
    assert (tmp_path / "config" / "models.json.bak").exists()
    # 前置备份存在，且不含密钥值
    backups = list((tmp_path / "backup").glob("vault-migration-*/models.json"))
    assert backups and "sk-old" not in backups[0].read_text(encoding="utf-8")


def test_migration_is_idempotent(tmp_path):
    store = seed_models(tmp_path)
    vault = make_vault(tmp_path)
    legacy = LegacyKeyring(backend=FakeLegacyBackend({"prv_1": "sk-old"}))
    migrate_keyring_to_vault(store, vault, legacy)

    again = migrate_keyring_to_vault(store, vault, legacy)
    assert again == {"migrated": 0, "skipped": 0, "failed": 0, "old_left": 0}


def test_migration_skips_when_legacy_missing(tmp_path):
    store = seed_models(tmp_path)
    vault = make_vault(tmp_path)
    legacy = LegacyKeyring(backend=FakeLegacyBackend({}))

    counts = migrate_keyring_to_vault(store, vault, legacy)
    assert counts["skipped"] == 1
    assert store.load("models").providers[0].key_ref == "keyring://ymt/prv_1"


def test_migration_failure_keeps_old_state(tmp_path):
    store = seed_models(tmp_path)
    vault = FakeVault(error="secret_write_failed")
    legacy = LegacyKeyring(backend=FakeLegacyBackend({"prv_1": "sk-old"}))

    counts = migrate_keyring_to_vault(store, vault, legacy)
    assert counts == {"migrated": 0, "skipped": 0, "failed": 1, "old_left": 0}
    assert store.load("models").providers[0].key_ref == "keyring://ymt/prv_1"  # 未改


def test_migration_readback_mismatch_fails(tmp_path):
    class LiarVault(FakeVault):
        def get(self, name):
            return "something-else"

    store = seed_models(tmp_path)
    legacy = LegacyKeyring(backend=FakeLegacyBackend({"prv_1": "sk-old"}))
    counts = migrate_keyring_to_vault(store, LiarVault(), legacy)
    assert counts["failed"] == 1
    assert store.load("models").providers[0].key_ref == "keyring://ymt/prv_1"


def test_migration_reports_old_entry_left(tmp_path):
    store = seed_models(tmp_path)
    vault = make_vault(tmp_path)
    backend = FakeLegacyBackend({"prv_1": "sk-old"})
    backend.delete_ok = False
    legacy = LegacyKeyring(backend=backend)

    counts = migrate_keyring_to_vault(store, vault, legacy)
    assert counts["migrated"] == 1 and counts["old_left"] == 1
    assert store.load("models").providers[0].key_ref == "vault://prv_1"


def test_migration_ignores_local_providers(tmp_path):
    store = seed_models(tmp_path, local=True, key_ref=None)
    vault = make_vault(tmp_path)
    legacy = LegacyKeyring(backend=FakeLegacyBackend({"prv_1": "sk-old"}))

    counts = migrate_keyring_to_vault(store, vault, legacy)
    assert counts == {"migrated": 0, "skipped": 0, "failed": 0, "old_left": 0}
    assert not vault.path.exists()


def test_migration_audit_never_records_secret(tmp_path):
    store = seed_models(tmp_path)
    vault = make_vault(tmp_path)
    seen: list[tuple] = []
    legacy = LegacyKeyring(backend=FakeLegacyBackend({"prv_1": "sk-super-secret"}))

    migrate_keyring_to_vault(store, vault, legacy, audit=lambda a, **f: seen.append((a, f)))
    assert [a for a, _ in seen] == ["vault_migrated"]
    assert "sk-super-secret" not in json.dumps(seen, ensure_ascii=False)


def test_migration_without_models_file_is_noop(tmp_path):
    store = ConfigStore(tmp_path)
    vault = make_vault(tmp_path)
    counts = migrate_keyring_to_vault(store, vault, LegacyKeyring(backend=FakeLegacyBackend({})))
    assert counts == {"migrated": 0, "skipped": 0, "failed": 0, "old_left": 0}


def test_legacy_keyring_fails_closed():
    class Boom:
        def get_password(self, *_a):
            raise RuntimeError("vault locked")

    legacy = LegacyKeyring(backend=Boom())
    assert legacy.get_key("prv_1") is None  # 不可用不得抛给调用方