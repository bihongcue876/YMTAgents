"""AI 对话有效性自检：离线配置体检（L1）+ 联网连通性探测（L2）。

定位：**测试侧回归资产**，不是产品功能（决策记录见 docs/B-阶段进度安排.md）。
把 BUILD 里手工填写的冒烟表格变成可重复执行的检查。

纪律：
- 只读 `ymtdata/`，不写配置、不自动修正（docs 09 P2 / P5）；
- 密钥只判状态、不回显（docs 09 B3）；
- 面向用户的提示取 `shared.errors.ERROR_TEXT`（spec rev5 §4）。

可自动判定的上限是「对话管线是否有效」（拿到非空回复、用量正常、事件完整）；
「回答得好不好」属评测范畴，不在本检查内。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.gateway.provider import ModelGateway
from core.gateway.whitelist import Whitelist, domain_of
from core.security.dpapi import DpapiBox
from core.security.vault import ISecretStore, Vault, secret_name
from core.store.config_store import ConfigStore
from shared.errors import ERROR_TEXT

SELFTEST_ENV = "YMT_SELFTEST"

# (ok, latency_ms, error_code)
Probe = Callable[[str, str], tuple]

_STATUS_TEXT = {"stored": "已存储", "missing": "未设置", "error": "加密库不可用"}


@dataclass(frozen=True)
class Check:
    """一条体检结论。ok=False 时必须给 hint —— 否则用户不知道下一步做什么。"""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""

    def render(self) -> str:
        line = f"[{'OK  ' if self.ok else 'FAIL'}] {self.name}"
        if self.detail:
            line += f" —— {self.detail}"
        if not self.ok and self.hint:
            line += f"\n         → {self.hint}"
        return line


def render_report(checks: list[Check]) -> str:
    failed = [c for c in checks if not c.ok]
    verdict = "对话管线有效" if not failed else f"对话管线不可用（{len(failed)} 项未通过）"
    body = "\n".join(c.render() for c in checks)
    return f"AI 对话有效性自检\n{'-' * 46}\n{body}\n{'-' * 46}\n结论：{verdict}"


def _err_text(code: str | None) -> str:
    code = code or ""
    return ERROR_TEXT.get(code, code or "未知错误")


def _conn_hint(code: str | None) -> str:
    """按错误码给下一步 —— 提示指错方向比没有提示更糟。"""
    if code == "model_not_found":
        return "核对模型 ID —— 该供应商端点不提供此模型"
    if code == "whitelist_blocked":
        return "在网络白名单中加入该供应商域名"
    if code in ("auth_error", "key_missing", "key_error"):
        return "重新输入 API Key（密钥只存本地加密库，不落明文）"
    if code == "protocol_error":
        return "核对 base_url 是否为 OpenAI 兼容端点"
    return "核对 base_url、API Key 与网络出口"


def _owner_of(models, model_id: str) -> tuple[str | None, str | None]:
    """模型 → 其所属供应商 id。"""
    for p in models.providers:
        if any(m.id == model_id for m in p.models):
            return p.id, model_id
    return None, None


# ---------------------------------------------------------------------------
# L1：离线配置体检（不联网、零成本）
# ---------------------------------------------------------------------------
def check_config(root: Path | str, secrets: ISecretStore | None = None) -> list[Check]:
    """只读现有配置做一致性检查。"""
    root = Path(root)
    store = ConfigStore(root)
    secrets = secrets or Vault(root, DpapiBox())
    models = store.load("models")
    settings = store.load("settings")
    whitelist = Whitelist(settings.network.whitelist)

    checks: list[Check] = []
    if not models.providers:
        checks.append(
            Check("供应商", False, "未配置任何供应商", "模型配置 → 添加供应商，并填入 API Key")
        )
        checks.append(
            Check("全局默认（main）", False, "无可绑定对象", "先添加供应商，再绑定 main 槽位")
        )
        return checks

    checks.append(Check("供应商", True, f"{len(models.providers)} 个"))
    for p in models.providers:
        label = f"供应商「{p.name}」"
        checks.append(
            Check(f"{label} · 地址", bool(p.base_url), p.base_url or "空", "填写 http(s) 端点")
        )
        checks.append(
            Check(
                f"{label} · 模型表",
                bool(p.models),
                " / ".join(m.id for m in p.models) or "空",
                "在供应商编辑对话框中登记模型 ID",
            )
        )
        status = secrets.status(secret_name(p.id))
        checks.append(
            Check(
                f"{label} · 凭据",
                status == "stored",
                _STATUS_TEXT.get(status, status),
                "重新输入 API Key（密钥只存本地加密库，不落明文）",
            )
        )
        checks.append(
            Check(
                f"{label} · 出口白名单",
                whitelist.is_allowed(p.base_url),
                domain_of(p.base_url) or p.base_url,
                "在网络白名单中加入该域名",
            )
        )

    main = models.slots.get("main")
    known = {m.id for p in models.providers for m in p.models}
    if not main:
        checks.append(
            Check("全局默认（main）", False, "未绑定", "模型配置 → 槽位绑定 → main 选择模型")
        )
    elif main not in known:
        checks.append(
            Check(
                "全局默认（main）",
                False,
                f"{main} 不在任何供应商的模型表中",
                "重新绑定 main，或把该模型登记到供应商",
            )
        )
    else:
        checks.append(Check("全局默认（main）", True, main))
    return checks


# ---------------------------------------------------------------------------
# L2：联网连通性探测（每个供应商一次 + 全局默认一次，成本可忽略）
# ---------------------------------------------------------------------------
def check_connectivity(root: Path | str, probe: Probe | None = None) -> list[Check]:
    """区分「供应商不通」与「模型不可用」——只看 L1 无法发现无效模型 ID。"""
    root = Path(root)
    store = ConfigStore(root)
    models = store.load("models")
    if probe is None:
        probe = ModelGateway(store).test_connection

    checks: list[Check] = []
    for p in models.providers:
        if not p.models:
            checks.append(
                Check(f"供应商「{p.name}」连通性", False, "无模型可探测", "先登记模型 ID")
            )
            continue
        mid = p.models[0].id
        ok, latency, err = probe(p.id, mid)
        checks.append(
            Check(
                f"供应商「{p.name}」连通性",
                ok,
                f"{mid} · {latency}ms" if ok else f"{mid} · {_err_text(err)}",
                _conn_hint(err),
            )
        )

    main = models.slots.get("main")
    if not main:
        checks.append(
            Check(
                "全局默认模型可用性",
                False,
                "未绑定 main，无法探测",
                "模型配置 → 槽位绑定 → main 选择模型",
            )
        )
        return checks

    pid, mid = _owner_of(models, main)
    if pid is None:
        checks.append(
            Check("全局默认模型可用性", False, f"{main} 不属于任何供应商", "重新绑定 main")
        )
        return checks

    ok, latency, err = probe(pid, mid)
    checks.append(
        Check(
            "全局默认模型可用性",
            ok,
            f"{main} · {latency}ms" if ok else f"{main} · {_err_text(err)}",
            _conn_hint(err),
        )
    )
    return checks


def run_selftest(root: Path | str) -> list[Check]:
    """完整自检（L1 + L2）。"""
    return check_config(root) + check_connectivity(root)
