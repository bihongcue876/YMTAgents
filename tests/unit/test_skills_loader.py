"""Skill 载体解析测试（v0.0.4 / spec §2.1）：frontmatter 解析与 fail-closed 校验。"""

from __future__ import annotations

from pathlib import Path

from core.skills.loader import MAX_BODY_CHARS, parse_skill_md

VALID = """---
name: 审阅
description: 按清单审阅文档。
version: 2
permission: safe
---
第一行指令
第二行指令
"""


def _write(tmp_path: Path, sid: str, text: str) -> Path:
    d = tmp_path / sid
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d / "SKILL.md"


def test_parse_valid(tmp_path):
    path = _write(tmp_path, "skl_a", VALID)
    meta, error, body = parse_skill_md(path)
    assert error is None
    assert meta is not None
    assert meta.id == "skl_a"
    assert meta.name == "审阅"
    assert meta.description == "按清单审阅文档。"
    assert meta.version == "2"
    assert meta.permission == "safe"
    assert body.startswith("第一行指令")


def test_permission_defaults_to_safe(tmp_path):
    path = _write(tmp_path, "skl_b", "---\nname: n\ndescription: d\n---\n正文")
    meta, error, _body = parse_skill_md(path)
    assert error is None and meta.permission == "safe"


def test_missing_frontmatter(tmp_path):
    path = _write(tmp_path, "skl_c", "没有围栏的正文")
    meta, error, _ = parse_skill_md(path)
    assert meta is None and "frontmatter" in error


def test_missing_name(tmp_path):
    path = _write(tmp_path, "skl_d", "---\ndescription: d\n---\n正文")
    meta, error, _ = parse_skill_md(path)
    assert meta is None and "name" in error


def test_missing_description(tmp_path):
    path = _write(tmp_path, "skl_e", "---\nname: n\n---\n正文")
    meta, error, _ = parse_skill_md(path)
    assert meta is None and "description" in error


def test_bad_permission(tmp_path):
    path = _write(tmp_path, "skl_f", "---\nname: n\ndescription: d\npermission: auto\n---\n正文")
    meta, error, _ = parse_skill_md(path)
    assert meta is None and "permission" in error


def test_empty_body(tmp_path):
    path = _write(tmp_path, "skl_g", "---\nname: n\ndescription: d\n---\n   ")
    meta, error, _ = parse_skill_md(path)
    assert meta is None and "正文为空" in error


def test_body_too_long(tmp_path):
    body = "字" * (MAX_BODY_CHARS + 1)
    path = _write(tmp_path, "skl_h", f"---\nname: n\ndescription: d\n---\n{body}")
    meta, error, _ = parse_skill_md(path)
    assert meta is None and "超长" in error


def test_bad_skill_id(tmp_path):
    path = _write(tmp_path, "bad.id", "---\nname: n\ndescription: d\n---\n正文")
    meta, error, _ = parse_skill_md(path)
    assert meta is None and "id 不合法" in error


def test_oversized_file(tmp_path):
    path = _write(tmp_path, "skl_i", "---\nname: n\ndescription: d\n---\n" + "x" * (2 * 1024 * 1024 + 1))
    meta, error, _ = parse_skill_md(path)
    assert meta is None and "过大" in error
