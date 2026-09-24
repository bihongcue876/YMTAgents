"""BTCM 配置解析与模型路由（切片 2）。

参数优先级（承原型）：`modules.json → btcm.agents.<name>` > 内置默认。
模型优先「本对话模型」（D4），其次页面指定槽位，最后全局默认。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared.schema import BtcmConfig

#: 宿主内置默认（承原型 `btcm.json` 的 Agent 参数默认值）。不下发 max_tokens（rev20）。
DEFAULT_AGENT_PARAMS: dict[str, dict[str, Any]] = {
    "creative": {"temperature": 0.8, "num_candidates": 3, "timeout": None},
    "validator": {"temperature": 0.3, "timeout": None},
    "controller": {"temperature": 0.3, "timeout": None},
    "meta": {"temperature": 0.3, "log_intermediate": True, "timeout": None},
}

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: 思考深度指令（承原型 `EFFORT_DIRECTIVES`）。light 另由引擎强制单轮。
EFFORT_DIRECTIVES: dict[str, str] = {
    "light": (
        "本轮思考深度要求：略想。快速给出可用的判断与结果，"
        "不展开长篇推理与铺陈分析，不为完备性补充冗余内容；宁可结论简短，不要深思。"
    ),
    "deep": (
        "本轮思考深度要求：深层。请充分深思：多角度检验逻辑与事实，"
        "主动挖掘漏洞与反例，严谨论证后再给出结论。"
    ),
}


def effort_directive(effort: str | None) -> str:
    directive = EFFORT_DIRECTIVES.get(effort or "", "")
    return directive + "\n" if directive else ""


def agent_params(config: BtcmConfig, name: str) -> dict[str, Any]:
    params = dict(DEFAULT_AGENT_PARAMS.get(name, {}))
    override = config.agents.get(name)
    if override is not None:
        params.update(override.model_dump(exclude_none=True))
    return params


def resolve_model(gateway: Any, session_model: str | None, slot: str) -> str | None:
    """D4 路由：本对话模型优先 → 指定槽位 → 全局默认。"""
    if session_model:
        return session_model
    slots = gateway.get_slots()
    return slots.get(slot) or slots.get("main")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()