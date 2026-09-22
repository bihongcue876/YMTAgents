"""工作区路径解析与禁设清单（v0.0.6 spec §3.3 / §3.13 R4/R5/R9）。

`layout.py` 是**纯函数**模块（零 IO）—— 因此这里可以数据驱动地把全部组合压完，
不需要任何文件系统准备。禁设清单逐条断言「拒绝 + 理由可读」，因为漏判的代价是
用户把磁盘根当工作区，而 shell 会在整盘范围内执行命令。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.workspace import layout


# -- root 解析 -----------------------------------------------------------------
def test_resolve_root_managed_is_relative_to_data_root(tmp_path):
    root = layout.resolve_root("managed", "workspace", tmp_path)
    assert root == layout.abs_path(tmp_path / "workspace")
    assert root.is_absolute()


def test_resolve_root_managed_rejects_absolute(tmp_path):
    with pytest.raises(layout.WorkspacePathError):
        layout.resolve_root("managed", str(tmp_path / "elsewhere"), tmp_path)


def test_resolve_root_managed_rejects_escape(tmp_path):
    """`..` 一律拒绝：托管路径不允许逃出数据根（否则「可迁移」的承诺就是假的）。"""
    for bad in ("../escape", "a/../../b", ".."):
        with pytest.raises(layout.WorkspacePathError):
            layout.resolve_root("managed", bad, tmp_path)


def test_resolve_root_external_is_absolute(tmp_path):
    assert layout.resolve_root("external", str(tmp_path), tmp_path) == layout.abs_path(tmp_path)


def test_resolve_root_external_requires_value(tmp_path):
    with pytest.raises(layout.WorkspacePathError):
        layout.resolve_root("external", "   ", tmp_path)


def test_resolve_root_unknown_kind(tmp_path):
    with pytest.raises(layout.WorkspacePathError):
        layout.resolve_root("nfs", "x", tmp_path)


# -- data_home 解析 -----------------------------------------------------------
def test_resolve_data_home_inline_ignores_stored_value(tmp_path):
    """`inline` 的实际落点恒为 `<root>/.ymtdata`，登记里的值只作记录。

    这样即便有人手编 index.json 把 inline 的值改成别的，也不会凭空多出一个落点
    （防手编漂移 —— 与「文件即配置」并存的必要约束）。
    """
    root = tmp_path / "ws"
    assert layout.resolve_data_home("inline", ".ymtdata", root, tmp_path) == layout.abs_path(
        root / ".ymtdata"
    )
    assert layout.resolve_data_home("inline", "somewhere/else", root, tmp_path) == layout.abs_path(
        root / ".ymtdata"
    )


def test_resolve_data_home_managed_relative_to_data_root(tmp_path):
    root = tmp_path / "ws"
    got = layout.resolve_data_home("managed", "workspaces/x/data", root, tmp_path)
    assert got == layout.abs_path(tmp_path / "workspaces/x/data")


def test_resolve_data_home_custom_is_absolute(tmp_path):
    root = tmp_path / "ws"
    other = tmp_path / "custom-home"
    assert layout.resolve_data_home("custom", str(other), root, tmp_path) == layout.abs_path(other)


def test_resolve_data_home_custom_requires_value(tmp_path):
    with pytest.raises(layout.WorkspacePathError):
        layout.resolve_data_home("custom", "", tmp_path / "ws", tmp_path)


# -- 路径同一性 ---------------------------------------------------------------
def test_path_key_is_case_insensitive():
    """Windows 语义：大小写不敏感。级联去重与撞车检测都依赖它，判错会重复注入记忆。"""
    assert layout.path_key(r"C:\Data\Ws") == layout.path_key(r"c:\data\ws")
    assert layout.same_path(r"C:\Data\Ws", r"c:\DATA\ws")


def test_is_under_boundaries(tmp_path):
    parent = tmp_path / "a"
    assert layout.is_under(parent / "b", parent)
    assert layout.is_under(parent, parent)  # 含自身
    assert not layout.is_under(tmp_path / "ab", parent)  # 前缀相似但不同目录


# -- 禁设清单（R5）------------------------------------------------------------
# 注意：这些用例里 data_root 刻意取 `tmp_path/data` 这个**子目录** ——
# 若直接把 tmp_path 当 data_root，所有候选目录都会先命中「数据根内」那条规则，
# 于是「磁盘根 / 主目录 / 数据目录名」等规则永远测不到（测试自己把自己挡住的典型）。
def _roots(tmp_path):
    return tmp_path / "data", tmp_path


def test_forbid_reason_rejects_disk_root(tmp_path):
    data_root, _ = _roots(tmp_path)
    anchor = Path(os.path.abspath(os.sep))
    reason = layout.forbid_reason(anchor, data_root)
    assert reason and "磁盘根" in reason


def test_forbid_reason_rejects_home(tmp_path):
    data_root, _ = _roots(tmp_path)
    reason = layout.forbid_reason(Path.home(), data_root)
    assert reason and "主目录" in reason


def test_forbid_reason_rejects_data_root_itself(tmp_path):
    data_root, _ = _roots(tmp_path)
    reason = layout.forbid_reason(data_root, data_root)
    assert reason and "数据根" in reason


def test_forbid_reason_rejects_inside_data_root_but_outside_managed_area(tmp_path):
    """数据根内的配置/会话区不许当工作区（会与配置、会话目录冲突）。"""
    data_root, _ = _roots(tmp_path)
    reason = layout.forbid_reason(data_root / "sessions", data_root)
    assert reason and "数据根内" in reason


def test_forbid_reason_allows_managed_area(tmp_path):
    """托管区是宿主自己分配的位置，必须放行 —— 否则托管工作区一个也建不出来。"""
    data_root, _ = _roots(tmp_path)
    assert layout.forbid_reason(data_root / "workspaces" / "ws_x", data_root) is None


def test_forbid_reason_rejects_data_home_itself(tmp_path):
    data_root, outside = _roots(tmp_path)
    reason = layout.forbid_reason(outside / "proj" / ".ymtdata", data_root)
    assert reason and ".ymtdata" in reason


def test_forbid_reason_allows_ordinary_directory(tmp_path):
    """正常开发目录必须放行（禁设清单宁缺毋滥：`C:////Projects////foo` 这类不该被拒）。"""
    data_root, outside = _roots(tmp_path)
    assert layout.forbid_reason(outside / "ordinary", data_root) is None


# -- safe_child（文件列举入口，R9 越界防护）------------------------------------
def test_safe_child_normal_and_empty(tmp_path):
    assert layout.safe_child(tmp_path, "src") == layout.abs_path(tmp_path / "src")
    assert layout.safe_child(tmp_path, "") == layout.abs_path(tmp_path)
    assert layout.safe_child(tmp_path, ".") == layout.abs_path(tmp_path)
    assert layout.safe_child(tmp_path, "a/b") == layout.abs_path(tmp_path / "a" / "b")


def test_safe_child_rejects_escape(tmp_path):
    for bad in ("..", "../x", "a/../../b"):
        with pytest.raises(layout.WorkspacePathError):
            layout.safe_child(tmp_path, bad)


def test_safe_child_rejects_absolute(tmp_path):
    with pytest.raises(layout.WorkspacePathError):
        layout.safe_child(tmp_path, str(tmp_path.parent))


# -- 级联去重 -----------------------------------------------------------------
def test_unique_paths_dedups_case_insensitively_and_keeps_order():
    got = layout.unique_paths([Path("C:/a"), Path("C:/b"), Path("c:/A"), Path("C:/b")])
    assert [str(p) for p in got] == ["C:\\a", "C:\\b"] or [p.name for p in got] == ["a", "b"]


def test_unique_paths_preserves_single_path():
    assert layout.unique_paths([Path("C:/only")]) == [Path("C:/only")]
