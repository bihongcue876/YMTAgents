"""AI 对话有效性自检用例。

- 离线用例（L1 / 注入式 L2）：**恒跑**，用合成数据根，不联网、不触碰真实配置。
- 真实配置自检：默认 **skip**，`YMT_SELFTEST=1 uv run pytest tests/selftest -v -s` 才执行。
  它的红/绿就是结论 —— 配置不全时失败是预期行为，不是用例缺陷。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import paths
from core.gateway.keyring_store import KeyringStore
from core.store.config_store import ConfigStore
from shared.schema import ModelConfig, ModelsConfig, ProviderConfig
from tests.selftest.checks import (
    SELFTEST_ENV,
    Check,
    check_config,
    check_connectivity,
    render_report,
    run_selftest,
)

needs_live = pytest.mark.skipif(
    not os.environ.get(SELFTEST_ENV),
    reason=f"真实配置自检需设置 {SELFTEST_ENV}=1",
)


class FakeKeyring:
    """不触碰系统凭据管理器（测试只关心状态判定）。"""

    def __init__(self, stored: bool = True) -> None:
        self.data = {"ymt": {"prv_x": "sk-x"}} if stored else {}

    def get_password(self, service: str, user: str):
        return self.data.get(service, {}).get(user)


def _write_root(root: Path, *, key: bool = True, bind_main: bool = True, whitelist: bool = True) -> Path:
    """合成数据根：两个模型（mx 为供应商首个，mx2 为全局默认），便于区分两种探测。"""
    store = ConfigStore(root)
    store.ensure_defaults()
    models = ModelsConfig(
        providers=[
            ProviderConfig(
                id="prv_x",
                name="X",
                base_url="https://api.x.com/v1",
                models=[
                    ModelConfig(id="mx", ctx_window=1000),
                    ModelConfig(id="mx2", ctx_window=2000),
                ],
            )
        ]
    )
    if bind_main:
        models.slots["main"] = "mx2"
    store.save("models", models)

    settings = store.load("settings")
    settings.network.whitelist = ["api.x.com"] if whitelist else []
    store.save("settings", settings)
    return root


# -- L1 离线体检 -------------------------------------------------------------


def test_empty_config_fails_with_actionable_hints(tmp_path):
    """零配置：必须报红，且每条都给出下一步（禁止白屏式失败）。"""
    checks = check_config(tmp_path, keyring=KeyringStore(backend=FakeKeyring()))
    assert checks and all(not c.ok for c in checks)
    assert all(c.hint for c in checks if not c.ok)
    assert "添加供应商" in render_report(checks)


def test_configured_root_passes_l1(tmp_path):
    _write_root(tmp_path)
    checks = check_config(tmp_path, keyring=KeyringStore(backend=FakeKeyring()))
    failed = [c for c in checks if not c.ok]
    assert not failed, render_report(checks)
    assert "对话管线有效" in render_report(checks)


def test_missing_key_and_whitelist_are_distinguished(tmp_path):
    _write_root(tmp_path, whitelist=False)
    checks = check_config(tmp_path, keyring=KeyringStore(backend=FakeKeyring(stored=False)))
    names = {c.name: c for c in checks}
    assert names["供应商「X」 · 凭据"].ok is False
    assert names["供应商「X」 · 凭据"].detail == "未设置"
    assert names["供应商「X」 · 出口白名单"].ok is False


def test_unbound_slot_is_reported_not_hidden(tmp_path):
    _write_root(tmp_path, bind_main=False)
    checks = check_config(tmp_path, keyring=KeyringStore(backend=FakeKeyring()))
    slot = next(c for c in checks if c.name == "全局默认（main）")
    assert slot.ok is False and "未绑定" in slot.detail


def test_slot_pointing_to_unknown_model_is_flagged(tmp_path):
    _write_root(tmp_path)
    store = ConfigStore(tmp_path)
    models = store.load("models")
    models.slots["main"] = "ghost-model"  # 引用完整性破坏
    store.save("models", models)

    checks = check_config(tmp_path, keyring=KeyringStore(backend=FakeKeyring()))
    slot = next(c for c in checks if c.name == "全局默认（main）")
    assert slot.ok is False and "不在任何供应商" in slot.detail


# -- L2 连通性（注入 probe，不联网） -----------------------------------------


def test_connectivity_separates_supplier_from_model(tmp_path):
    """回归锚点：L1 看不到「模型 ID 端点不认」，L2 必须把两种失败区分开。

    供应商首个模型 mx 探测通过 → 说明供应商与凭据都通；
    全局默认 mx2 探测失败 → 说明问题只在这一个模型，而不是供应商。
    """
    _write_root(tmp_path)

    def probe(provider_id: str, model_id: str):
        assert provider_id == "prv_x"
        return (True, 12, None) if model_id == "mx" else (False, None, "model_not_found")

    checks = check_connectivity(tmp_path, probe=probe)
    assert [c.name for c in checks] == ["供应商「X」连通性", "全局默认模型可用性"]
    assert checks[0].ok is True and "mx · 12ms" in checks[0].detail
    assert checks[1].ok is False and "mx2" in checks[1].detail
    assert "该模型在供应商不可用" in checks[1].detail


def test_connectivity_reports_chinese_reason(tmp_path):
    _write_root(tmp_path)

    def probe(_provider_id: str, _model_id: str):
        return False, None, "model_not_found"

    checks = check_connectivity(tmp_path, probe=probe)
    assert all(not c.ok for c in checks)
    assert all("该模型在供应商不可用" in c.detail for c in checks)


def test_connectivity_without_binding_does_not_probe(tmp_path):
    _write_root(tmp_path, bind_main=False)
    calls: list[tuple[str, str]] = []

    def probe(provider_id: str, model_id: str):
        calls.append((provider_id, model_id))
        return True, 1, None

    checks = check_connectivity(tmp_path, probe=probe)
    slot = next(c for c in checks if c.name == "全局默认模型可用性")
    assert slot.ok is False and "未绑定" in slot.detail
    assert calls == [("prv_x", "mx")]  # 只探了供应商级，没探槽位


def test_connectivity_with_no_provider(tmp_path):
    checks = check_connectivity(tmp_path, probe=lambda *_: (True, 1, None))
    slot = next(c for c in checks if c.name == "全局默认模型可用性")
    assert slot.ok is False


# -- 真实配置自检（opt-in） --------------------------------------------------


@needs_live
def test_live_conversation_pipeline():
    """对真实 ymtdata/ 跑 L1+L2。失败即结论：当前配置下对话管线不可用。"""
    root = paths.data_root()
    checks = run_selftest(root)
    print("\n" + render_report(checks))
    assert all(c.ok for c in checks), render_report(checks)


@needs_live
def test_live_selftest_is_read_only():
    """自检不得改动任何配置数据（对比运行前后）。"""
    root = paths.data_root()
    files = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    run_selftest(root)
    after = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    assert files == after


def test_check_dataclass_renders_hint():
    text = Check("某检查", False, "详情", "去做这件事").render()
    assert "FAIL" in text and "详情" in text and "去做这件事" in text
