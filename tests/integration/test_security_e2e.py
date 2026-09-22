"""密钥加密端到端验收（spec v0.0.2 §9 E1/E2/E3/E7）。"""

from __future__ import annotations

import json

from app import bootstrap as bootstrap_mod
from app import paths
from core.gateway.provider import ModelGateway
from core.security.dpapi import ENTROPY_VAULT
from core.security.legacy import LegacyKeyring
from core.security.vault import Vault, key_ref_for, secret_name
from core.store.config_store import ConfigStore
from shared.envelope import ModelSpec, ProviderSpec
from shared.schema import ModelConfig, ModelsConfig, ProviderConfig
from tests.mocks.secrets import FakeBox, FakeVault


def _scan(root, needle: str) -> list[str]:
    """返回数据根下含明文 needle 的文件（E2 的核心断言手段）。"""
    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if needle.encode("utf-8") in path.read_bytes():
                hits.append(str(path.relative_to(root)))
        except OSError:
            continue
    return hits


def test_vault_blob_roundtrips_without_plaintext(tmp_path):
    vault = Vault(tmp_path, FakeBox())
    vault.set(secret_name("prv_1"), "sk-abc-123")

    raw = vault.path.read_text(encoding="utf-8")
    assert ENTROPY_VAULT.decode() not in raw
    assert "sk-abc-123" not in raw

    fresh = Vault(tmp_path, FakeBox())
    assert fresh.get(secret_name("prv_1")) == "sk-abc-123"
    assert _scan(tmp_path, "sk-abc-123") == []


def _seed_store(root) -> ConfigStore:
    store = ConfigStore(root)
    store.ensure_defaults()
    models = ModelsConfig()
    models.providers.append(
        ProviderConfig(
            id="prv_1",
            name="P",
            base_url="https://api.test.com/v1",
            key_ref="keyring://ymt/prv_1",
            models=[ModelConfig(id="m1", ctx_window=1000)],
        )
    )
    store.save("models", models)
    return store


def test_gateway_writes_only_vault_reference(tmp_path, monkeypatch):
    """E1：upsert 供应商后，models.json 只有 vault:// 引用，密钥只在加密库。"""
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    root = tmp_path / "ymtdata"
    store = ConfigStore(root)
    store.ensure_defaults()
    vault = Vault(root, FakeBox())
    gateway = ModelGateway(store, secrets=vault)

    req = ProviderSpec(
        id="prv_1",
        name="P",
        base_url="https://api.test.com/v1",
        models=[ModelSpec(id="m1", ctx_window=1000)],
    )
    gateway.upsert_provider(req, "sk-abc-123")

    saved = json.loads((root / "config" / "models.json").read_text(encoding="utf-8"))
    assert saved["providers"][0]["key_ref"] == key_ref_for("prv_1")
    assert gateway.list_providers()[0].key_status == "stored"
    assert vault.get(secret_name("prv_1")) == "sk-abc-123"

    # E2：全数据根（含 .bak / backup/）不得出现明文密钥
    assert _scan(root, "sk-abc-123") == []


def test_gateway_delete_provider_removes_secret(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_defaults()
    vault = Vault(tmp_path, FakeBox())
    gateway = ModelGateway(store, secrets=vault)
    gateway.upsert_provider(
        ProviderSpec(id="prv_1", name="P", base_url="https://api.test.com/v1"), "sk-abc"
    )

    gateway.delete_provider("prv_1")
    assert vault.status(secret_name("prv_1")) == "missing"


def _bootstrap(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(**kwargs)


def test_bootstrap_migrates_legacy_key_and_never_stores_plaintext(tmp_path, monkeypatch, qapp):
    _seed_store(tmp_path / "ymtdata")
    vault = FakeVault()
    legacy = LegacyKeyring(backend=_FakeLegacy({"prv_1": "sk-legacy-999"}))

    ctx = _bootstrap(tmp_path, monkeypatch, secrets=vault, legacy=legacy)
    try:
        models = ConfigStore(ctx.root).load("models")
        assert models.providers[0].key_ref == key_ref_for("prv_1")
        assert vault.get(secret_name("prv_1")) == "sk-legacy-999"
        assert _scan(ctx.root, "sk-legacy-999") == []

        audit = (ctx.root / "logs" / "audit.jsonl").read_text(encoding="utf-8")
        assert "vault_migrated" in audit
        assert "sk-legacy-999" not in audit
    finally:
        ctx.worker.stop()


def test_bootstrap_continues_when_migration_fails(tmp_path, monkeypatch, qapp):
    """E7：迁移失败不阻断启动，旧引用保持原样。"""
    _seed_store(tmp_path / "ymtdata")
    vault = FakeVault(error="secret_write_failed")
    backend = _FakeLegacy({"prv_1": "sk-legacy-999"})
    legacy = LegacyKeyring(backend=backend)

    ctx = _bootstrap(tmp_path, monkeypatch, secrets=vault, legacy=legacy)
    try:
        models = ConfigStore(ctx.root).load("models")
        assert models.providers[0].key_ref == "keyring://ymt/prv_1"
        assert ctx.gateway is not None
        assert _scan(ctx.root, "sk-legacy-999") == []
    finally:
        ctx.worker.stop()


def test_bootstrap_is_idempotent_for_vault_refs(tmp_path, monkeypatch, qapp):
    _seed_store(tmp_path / "ymtdata")
    vault = FakeVault()
    backend = _FakeLegacy({"prv_1": "sk-legacy-999"})
    backend.delete_ok = False  # 旧条目删不掉也不该重试写
    legacy = LegacyKeyring(backend=backend)

    ctx = _bootstrap(tmp_path, monkeypatch, secrets=vault, legacy=legacy)
    try:
        ctx2 = _bootstrap(tmp_path, monkeypatch, secrets=vault, legacy=legacy)
        try:
            assert ConfigStore(ctx2.root).load("models").providers[0].key_ref == key_ref_for(
                "prv_1"
            )
            assert vault.get(secret_name("prv_1")) == "sk-legacy-999"
        finally:
            ctx2.worker.stop()
    finally:
        ctx.worker.stop()


class _FakeLegacy:
    """`LegacyKeyring` 的 backend 替身（凭据管理器不落明文，只存内存）。"""

    def __init__(self, data: dict[str, str]) -> None:
        self.data = dict(data)
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