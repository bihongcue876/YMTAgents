"""core.files.manager：五个内置文件工具的装配、策略裁决与执行（v0.0.11 切片 A/B）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.schema import BuiltinToolConfig

from core.files import manager as files_manager
from core.files.manager import (
    PROMPT_BLOCK_LIMIT,
    TOOL_EDIT,
    TOOL_GLOB,
    TOOL_GREP,
    TOOL_NAMES,
    TOOL_READ,
    TOOL_WRITE,
    FilesTools,
)
from core.registry.executor import ToolContext
from core.registry.registry import Registry


class _Store:
    def __init__(self, builtin=None):
        self._plugins = SimpleNamespace(builtin=builtin or {})

    def load(self, kind):
        assert kind == "plugins"
        return self._plugins


def _setup(tmp_path: Path, builtin=None):
    data = tmp_path / "ymtdata"
    root = data / "workspace"
    root.mkdir(parents=True)
    registry = Registry()
    audit: list[tuple] = []
    tools = FilesTools(
        registry,
        _Store(builtin),
        session_root=lambda sid: root,
        data_root=data,
        audit=lambda action, **fields: audit.append((action, fields)),
    )
    tools.set_active_session("s1")
    tools.refresh()
    return tools, registry, root, data, audit


def _spec(registry: Registry, name: str):
    return next(spec for spec in registry.snapshot() if spec.name == name)


def _ctx() -> ToolContext:
    return ToolContext(session_id="s1")


def test_registers_five_tools_with_prompt_blocks(tmp_path):
    _tools, registry, _root, _data, _audit = _setup(tmp_path)
    names = {spec.name for spec in registry.snapshot()}
    assert names == set(TOOL_NAMES)
    for spec in registry.snapshot():
        assert spec.permission.value == "confirm"
        assert 0 < len(spec.prompt_block) <= PROMPT_BLOCK_LIMIT


def test_disabled_tool_is_not_registered(tmp_path):
    builtin = {TOOL_GREP: BuiltinToolConfig(enabled=False)}
    _tools, registry, _root, _data, _audit = _setup(tmp_path, builtin)
    assert TOOL_GREP not in {spec.name for spec in registry.snapshot()}
    assert len(registry.snapshot()) == len(TOOL_NAMES) - 1


def test_oversized_prompt_block_fails_closed(tmp_path, monkeypatch):
    tools, _registry, _root, _data, _audit = _setup(tmp_path)
    monkeypatch.setitem(files_manager.PROMPT_BLOCKS, TOOL_READ, "x" * (PROMPT_BLOCK_LIMIT + 1))
    tools.shutdown()
    with pytest.raises(ValueError):
        tools.refresh()


def test_precheck_inside_read_is_allowed(tmp_path):
    tools, registry, root, _data, _audit = _setup(tmp_path)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    verdict = _spec(registry, TOOL_READ).precheck({"path": "a.txt"})
    assert verdict == ("allow", "")


def test_precheck_outside_read_needs_review(tmp_path):
    _tools, registry, _root, _data, _audit = _setup(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    assert _spec(registry, TOOL_READ).precheck({"path": str(outside)}) is None


def test_precheck_denies_secret_filename_and_outside_write(tmp_path):
    _tools, registry, _root, _data, _audit = _setup(tmp_path)
    assert _spec(registry, TOOL_READ).precheck({"path": ".env"})[0] == "deny"
    outside = str(tmp_path / "outside.txt")
    assert _spec(registry, TOOL_WRITE).precheck({"path": outside})[0] == "deny"


def test_precheck_warns_when_writing_without_reading_first(tmp_path):
    _tools, registry, root, _data, _audit = _setup(tmp_path)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    verdict = _spec(registry, TOOL_EDIT).precheck({"path": "a.txt"})
    assert verdict is not None and verdict[0] == "warn"


def test_reading_clears_the_warning(tmp_path):
    _tools, registry, root, _data, _audit = _setup(tmp_path)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    assert registry.execute(TOOL_READ, {"path": "a.txt"}, _ctx()).ok is True
    assert _spec(registry, TOOL_EDIT).precheck({"path": "a.txt"}) is None


def test_read_tool_returns_numbered_output(tmp_path):
    _tools, registry, root, _data, audit = _setup(tmp_path)
    (root / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    result = registry.execute(TOOL_READ, {"path": "a.txt"}, _ctx())
    assert result.ok is True
    assert "1| hello" in result.output and "2| world" in result.output
    assert any(action == "file.read" for action, _fields in audit)


def test_edit_error_codes_are_distinct(tmp_path):
    _tools, registry, root, _data, _audit = _setup(tmp_path)
    (root / "a.txt").write_text("same\nsame\n", encoding="utf-8")
    missing = registry.execute(TOOL_EDIT, {"path": "a.txt", "old_string": "absent", "new_string": "x"}, _ctx())
    ambiguous = registry.execute(TOOL_EDIT, {"path": "a.txt", "old_string": "same", "new_string": "x"}, _ctx())
    assert missing.ok is False and missing.error["code"] == "edit_no_match"
    assert ambiguous.ok is False and ambiguous.error["code"] == "edit_ambiguous"


def test_read_missing_file_maps_to_file_not_found(tmp_path):
    _tools, registry, _root, _data, _audit = _setup(tmp_path)
    result = registry.execute(TOOL_READ, {"path": "nope.txt"}, _ctx())
    assert result.ok is False and result.error["code"] == "file_not_found"


def test_write_then_edit_roundtrip(tmp_path):
    _tools, registry, root, _data, _audit = _setup(tmp_path)
    write = registry.execute(TOOL_WRITE, {"path": "new.txt", "content": "alpha\nbeta\n"}, _ctx())
    assert write.ok is True and "新建" in write.output
    edit = registry.execute(TOOL_EDIT, {"path": "new.txt", "old_string": "beta", "new_string": "BETA"}, _ctx())
    assert edit.ok is True and "+1" in edit.output
    assert (root / "new.txt").read_text(encoding="utf-8") == "alpha\nBETA\n"


def test_glob_and_grep_tools(tmp_path):
    _tools, registry, root, _data, _audit = _setup(tmp_path)
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("TARGET\n", encoding="utf-8")
    (root / "top.py").write_text("nothing\n", encoding="utf-8")
    globbed = registry.execute(TOOL_GLOB, {"pattern": "*.py"}, _ctx())
    assert globbed.ok is True and "top.py" in globbed.output and "pkg/mod.py" in globbed.output
    grepped = registry.execute(TOOL_GREP, {"pattern": "TARGET", "include": "*.py"}, _ctx())
    assert grepped.ok is True and "pkg/mod.py:1:" in grepped.output


def test_outside_read_audit_records_marker_only(tmp_path):
    """安全修订轮：区外读取审计只记标记，不把用户磁盘布局写进 audit.jsonl。"""
    _tools, registry, _root, _data, audit = _setup(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    result = registry.execute(TOOL_READ, {"path": str(outside)}, _ctx())
    assert result.ok is True
    entry = next(fields for action, fields in audit if action == "file.read")
    assert entry["inside"] is False
    assert entry["path"] == "（工作区外）"
