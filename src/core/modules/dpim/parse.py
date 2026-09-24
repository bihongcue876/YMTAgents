"""DPIM 结构化输出解析：只接受 JSON 对象，解析错误不回显模型原文。"""

from __future__ import annotations

from shared.json_parse import parse_json_object as _parse_json_object


class AgentOutputError(ValueError):
    """模型结构化输出不可用。"""


def parse_object(text: str) -> dict:
    try:
        return _parse_json_object(text)
    except ValueError as exc:
        raise AgentOutputError("模型结构化输出无法解析。") from exc
