"""BTCM 结构化输出解析（承原型 `agents/base.py`，同步重写）。

支持 ```json 围栏与正文截取 `{...}` 兜底；解析失败抛 `AgentOutputError`，
由调用方按语义处理（validator 降级 fail，其余转内部错误）。
"""

from __future__ import annotations

from shared.json_parse import parse_json_object as _parse_json_object


class AgentOutputError(Exception):
    """Agent 结构化输出解析失败（重试后仍失败）。"""


def parse_json_object(text: str) -> dict:
    try:
        return _parse_json_object(text)
    except ValueError as exc:
        raise AgentOutputError("无法从模型输出中解析 JSON 对象。") from exc


def require_keys(obj: dict, keys: list[str], agent_name: str) -> None:
    missing = [k for k in keys if k not in obj]
    if missing:
        raise AgentOutputError(f"Agent '{agent_name}' 输出缺少字段：{missing}")
