"""工作区宿主单元测试（v0.0.6 spec §3.3 / §3.13）。

重点不在「能建能删」，而在**承诺是否真的成立**：
- `ws_default` 的物理路径必须与 v0.0.5 及以前一字不差（存量数据零迁移）；
- 移除登记**绝不能碰磁盘**（R2）—— 这里建真实目录、真删登记、再断言目录仍在；
- 禁设清单与默认工作区不可动是**宿主层**保证（不只在界面拦，R3/R5）；
- 文件列举必须有界且不跟随链接（R9），否则一个巨大目录能把界面拖死。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.bus.sink import EventSink
from core.store.config_store import ConfigStore
from core.workspace import layout
from core.workspace.manager import IWorkspaceManager, WorkspaceManager
from core.workspace.layout import WorkspaceDenied, WorkspacePathError
from shared.ids import WS_DEFAULT


def _manager(tmp_path):
    root = tmp_path / "ymtdata"
    (root / "workspaces").mkdir(parents=True)
    store = ConfigStore(root)
    store.ensure_defaults()
    sink = EventSink(root)
    manager = WorkspaceManager(root, store, audit=sink.append_audit)
    manager.load()
    return root, store, manager


def _audit_actions(root: Path) -> list[str]:
    path = root / "logs" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["action"] for line in path.read_text(encoding="utf-8").splitlines() if line]


# -- 默认工作区与向后兼容 ------------------------------------------------------
def test_load_creates_default_workspace(tmp_path):
    root, _, manager = _manager(tmp_path)
    records = json.loads((root / "workspaces" / "index.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in records["workspaces"]] == [WS_DEFAULT]
    assert manager.current() == WS_DEFAULT


def test_default_root_and_data_home_match_legacy_shape(tmp_path):
    """**向后兼容锚点**：默认工作区的目录与数据落点必须重合于 `ymtdata/workspace/`。

    这是 v0.0.6 唯一不许动的既成事实 —— 现存 `AGENTS.md` / `manifest.json` / `files/`
    都要原样继续有效，不能因为引入「数据落点」概念就搬到别处。
    """
    root, _, manager = _manager(tmp_path)
    assert manager.root_of(WS_DEFAULT) == layout.abs_path(root / "workspace")
    assert manager.data_home_of(WS_DEFAULT) == layout.abs_path(root / "workspace")
    assert (root / "workspace" / "AGENTS.md").exists()
    assert (root / "workspace" / "manifest.json").exists()


def test_load_is_idempotent_and_keeps_extra_workspaces(tmp_path):
    root, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    manager.create("项目", root_kind="external", root=str(ext))
    before = (root / "workspaces" / "index.json").read_text(encoding="utf-8")
    manager.load()
    assert (root / "workspaces" / "index.json").read_text(encoding="utf-8") == before


def test_load_recovers_from_corrupt_index(tmp_path):
    """手编坏登记表不该让应用起不来：按空表加载 + 可读提示（文件即配置的兜底）。"""
    root, _, manager = _manager(tmp_path)
    (root / "workspaces" / "index.json").write_text("{ 这不是 JSON", encoding="utf-8")
    manager.load()
    assert manager.last_error()
    assert [item["id"] for item in manager.list_status()] == [WS_DEFAULT]


def test_index_without_default_row_gets_default_back(tmp_path):
    root, _, manager = _manager(tmp_path)
    (root / "workspaces" / "index.json").write_text(
        json.dumps({"schema_version": 1, "workspaces": []}), encoding="utf-8"
    )
    manager.load()
    assert manager.exists(WS_DEFAULT)
    # 补写的默认行要落盘，否则下次启动又要补一遍
    assert WS_DEFAULT in (root / "workspaces" / "index.json").read_text(encoding="utf-8")


# -- 创建 ---------------------------------------------------------------------
def test_create_external_workspace_makes_private_data_home(tmp_path):
    """用户裁决 D3：默认在工作区**之中**建 `.ymtdata` 作为记忆与文档落点。"""
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    wid = manager.create("项目", root_kind="external", root=str(ext))
    assert wid.startswith("ws_")
    home = ext / ".ymtdata"
    assert home.is_dir()
    assert (home / "documents").is_dir()
    assert (home / "artifacts").is_dir()
    assert (home / "AGENTS.md").exists()
    assert (home / "manifest.json").exists()
    assert manager.root_of(wid) == layout.abs_path(ext)
    assert manager.data_home_of(wid) == layout.abs_path(home)


def test_create_uses_settings_default_data_home_kind(tmp_path):
    """`settings.workspace.default_data_home_kind` 必须真的生效（不是摆设配置）。"""
    root, store, manager = _manager(tmp_path)
    settings = store.load("settings")
    settings.workspace.default_data_home_kind = "managed"
    store.save("settings", settings)
    wid = manager.create("托管落点")
    assert manager.data_home_of(wid) == layout.abs_path(root / "workspaces" / wid / "data")


def test_create_managed_workspace_lives_in_managed_area(tmp_path):
    root, _, manager = _manager(tmp_path)
    wid = manager.create("内托管")
    root_path = manager.root_of(wid)
    assert layout.is_under(root_path, root / "workspaces")
    assert root_path.is_dir()


def test_create_trims_and_limits_name(tmp_path):
    _, _, manager = _manager(tmp_path)
    wid = manager.create("  多余   空白  ")
    assert manager.list_status()[1]["name"] == "多余 空白"
    long_id = manager.create("名" * 200)
    assert len(manager.list_status()[-1]["name"]) == 60
    assert long_id != wid


def test_create_rejects_empty_name(tmp_path):
    _, _, manager = _manager(tmp_path)
    with pytest.raises(WorkspacePathError):
        manager.create("   ")


def test_create_rejects_missing_external_directory(tmp_path):
    _, _, manager = _manager(tmp_path)
    with pytest.raises(WorkspacePathError):
        manager.create("不存在", root_kind="external", root=str(tmp_path / "nope"))


def test_create_rejects_external_without_value(tmp_path):
    _, _, manager = _manager(tmp_path)
    with pytest.raises(WorkspacePathError):
        manager.create("空目录", root_kind="external", root=None)


def test_create_rejects_forbidden_locations(tmp_path):
    """R5 禁设清单在**宿主层**拒绝（界面绕不过去），且理由可读、归 `denied`。"""
    root, _, manager = _manager(tmp_path)
    for bad in (Path(os.path.abspath(os.sep)), Path.home(), root):
        with pytest.raises(WorkspaceDenied):
            manager.create("禁设", root_kind="external", root=str(bad))


def test_create_rejects_duplicate_directory(tmp_path):
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    manager.create("甲", root_kind="external", root=str(ext))
    with pytest.raises(WorkspacePathError) as excinfo:
        manager.create("乙", root_kind="external", root=str(ext))
    assert "已被工作区" in str(excinfo.value)


# -- 切换与查询 ---------------------------------------------------------------
def test_switch_unknown_raises(tmp_path):
    _, _, manager = _manager(tmp_path)
    with pytest.raises(KeyError):
        manager.switch("ws_nope")


def test_switch_and_counts_in_list_status(tmp_path):
    _, _, manager = _manager(tmp_path)
    wid = manager.create("项目")
    manager.switch(wid)
    assert manager.current() == wid
    status = {item["id"]: item for item in manager.list_status({wid: 3})}
    assert status[wid]["current"] is True
    assert status[wid]["sessions"] == 3
    assert status[WS_DEFAULT]["current"] is False


def test_list_status_marks_missing_root(tmp_path):
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    wid = manager.create("项目", root_kind="external", root=str(ext))
    ext.rmdir()
    status = {item["id"]: item for item in manager.list_status()}
    assert status[wid]["missing"] is True


def test_external_root_is_not_recreated_when_missing(tmp_path):
    """外部目录消失后，改名/改备注**不得**把它悄悄重建 —— 重建等于抹掉「已丢失」信号。"""
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    wid = manager.create("项目", root_kind="external", root=str(ext))
    ext.rmdir()

    manager.update(wid, name="改名了")

    assert not ext.exists()  # 不重建
    assert not (ext / ".ymtdata").exists()  # 也不写数据落点
    item = next(i for i in manager.list_status() if i["id"] == wid)
    assert item["missing"] is True
    assert item["name"] == "改名了"  # 改名本身要生效


def test_managed_root_is_recreated_when_missing(tmp_path):
    """托管目录相反：它是宿主分配的位置，重建是正确的。"""
    import shutil

    _, _, manager = _manager(tmp_path)
    wid = manager.create("托管")
    shutil.rmtree(manager.root_of(wid))
    manager.update(wid, name="还是托管")
    assert manager.root_of(wid).is_dir()


def test_list_status_keeps_records_with_broken_path_encoding(tmp_path):
    """手编坏编码不得让工作区从列表静默消失（否则用户无从知道该改哪一行）。"""
    root, _, manager = _manager(tmp_path)
    import json as _json

    index = root / "workspaces" / "index.json"
    data = _json.loads(index.read_text(encoding="utf-8"))
    data["workspaces"].append(
        {
            "id": "ws_broken",
            "name": "坏编码",
            "root_kind": "managed",
            "root": "../../escape",
            "data_home_kind": "inline",
            "data_home": ".ymtdata",
            "created_at": "2026-09-22T00:00:00Z",
        }
    )
    index.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    manager.load()

    item = next(i for i in manager.list_status() if i["id"] == "ws_broken")
    assert item["name"] == "坏编码"
    assert item["missing"] is True
    assert item["root"] == ""


def test_migratable_only_for_fully_managed(tmp_path):
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    external = manager.create("外部", root_kind="external", root=str(ext))
    managed = manager.create("托管")
    status = {item["id"]: item for item in manager.list_status()}
    assert status[WS_DEFAULT]["migratable"] is True
    assert status[managed]["migratable"] is True  # 托管目录 + inline 落点都随数据根走
    assert status[external]["migratable"] is False  # 外部目录搬不走


# -- 级联目录（去重是正确性点，不是优化）--------------------------------------
def test_cascade_dirs_dedups_when_current_is_default(tmp_path):
    """`active == default` 时两层是同一物理文件 → 只能注入一次，否则记忆重复占 token。"""
    _, _, manager = _manager(tmp_path)
    assert len(manager.cascade_dirs(None)) == 1
    assert len(manager.cascade_dirs(WS_DEFAULT)) == 1


def test_cascade_dirs_orders_default_then_current(tmp_path):
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    wid = manager.create("项目", root_kind="external", root=str(ext))
    dirs = manager.cascade_dirs(wid)
    assert dirs == [manager.data_home_of(WS_DEFAULT), manager.data_home_of(wid)]


# -- 更新 ---------------------------------------------------------------------
def test_update_saves_meta_and_build_cmd(tmp_path):
    _, _, manager = _manager(tmp_path)
    wid = manager.create("原名")
    manager.update(wid, name="新名", note="备注", build_cmd="npm run build")
    item = next(i for i in manager.list_status() if i["id"] == wid)
    assert item["name"] == "新名"
    assert item["note"] == "备注"
    assert item["build_cmd"] == "npm run build"


def test_create_persists_build_cmd(tmp_path):
    """建的时候就要能填构建命令 —— 界面已有输入框，契约不收就是静默丢数据。"""
    _, _, manager = _manager(tmp_path)
    wid = manager.create("项目", build_cmd="npm run build")
    assert next(i for i in manager.list_status() if i["id"] == wid)["build_cmd"] == "npm run build"


def test_create_truncates_overlong_build_cmd(tmp_path):
    from core.workspace.manager import MAX_BUILD_CMD

    _, _, manager = _manager(tmp_path)
    wid = manager.create("项目", build_cmd="x" * (MAX_BUILD_CMD + 500))
    item = next(i for i in manager.list_status() if i["id"] == wid)
    assert len(item["build_cmd"]) == MAX_BUILD_CMD


def test_update_default_workspace_allows_meta_but_not_relocation(tmp_path):
    """R3：默认工作区可改名/可设构建命令，但**不可搬 root、不可换落点**（宿主层拦）。"""
    _, _, manager = _manager(tmp_path)
    manager.update(WS_DEFAULT, name="我的默认")
    assert manager.list_status()[0]["name"] == "我的默认"
    ext = tmp_path / "proj"
    ext.mkdir()
    with pytest.raises(WorkspaceDenied):
        manager.update(WS_DEFAULT, name="默认工作区", root=str(ext))
    with pytest.raises(WorkspaceDenied):
        manager.update(WS_DEFAULT, name="默认工作区", data_home_kind="custom", data_home=str(ext))


def test_update_can_change_external_root_and_validates_it(tmp_path):
    _, _, manager = _manager(tmp_path)
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    wid = manager.create("项目", root_kind="external", root=str(first))
    manager.update(wid, name="项目", root=str(second))
    assert manager.root_of(wid) == layout.abs_path(second)
    with pytest.raises(WorkspacePathError):
        manager.update(wid, name="项目", root=str(tmp_path / "nope"))


def test_update_switching_to_custom_home_requires_value(tmp_path):
    _, _, manager = _manager(tmp_path)
    wid = manager.create("项目")
    with pytest.raises(WorkspacePathError):
        manager.update(wid, name="项目", data_home_kind="custom", data_home="")


# -- 移除（R2：不删磁盘）------------------------------------------------------
def test_delete_refuses_default_workspace(tmp_path):
    _, _, manager = _manager(tmp_path)
    with pytest.raises(WorkspaceDenied):
        manager.delete(WS_DEFAULT)


def test_delete_removes_record_only_and_keeps_disk_intact(tmp_path):
    """**R2 的核心断言**：建真实目录 → 真删登记 → 目录与 `.ymtdata` 必须都还在。"""
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "precious"
    ext.mkdir()
    (ext / "keep.txt").write_text("别删我", encoding="utf-8")
    wid = manager.create("珍贵的项目", root_kind="external", root=str(ext))
    home = ext / ".ymtdata"
    assert home.is_dir()

    manager.delete(wid)

    assert ext.is_dir()
    assert (ext / "keep.txt").read_text(encoding="utf-8") == "别删我"
    assert home.is_dir()
    assert (home / "AGENTS.md").exists()
    assert not manager.exists(wid)


def test_delete_current_workspace_falls_back_to_default(tmp_path):
    _, _, manager = _manager(tmp_path)
    wid = manager.create("项目")
    manager.switch(wid)
    manager.delete(wid)
    assert manager.current() == WS_DEFAULT


def test_delete_cleans_collapsed_state(tmp_path):
    """移除登记时一并清理折叠态，否则 settings 会留下指向不存在工作区的残留 id。"""
    _, store, manager = _manager(tmp_path)
    wid = manager.create("项目")
    manager.collapse(wid, True)
    assert wid in manager.collapsed()
    manager.delete(wid)
    assert wid not in manager.collapsed()
    assert manager.collapsed() == store.load("settings").ui.collapsed_workspaces


def test_delete_unknown_raises(tmp_path):
    _, _, manager = _manager(tmp_path)
    with pytest.raises(KeyError):
        manager.delete("ws_nope")


# -- 折叠态 -------------------------------------------------------------------
def test_collapse_roundtrip_persists_to_settings(tmp_path):
    _, store, manager = _manager(tmp_path)
    wid = manager.create("项目")
    assert manager.collapse(wid, True) == [wid]
    assert store.load("settings").ui.collapsed_workspaces == [wid]
    assert manager.collapse(wid, True) == [wid]  # 幂等：不重复追加
    assert manager.collapse(wid, False) == []


# -- 文件列举（有界 + 不跟随链接）---------------------------------------------
def test_detail_lists_entries_with_relative_paths(tmp_path):
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    (ext / "src").mkdir(parents=True)
    (ext / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (ext / "README.md").write_text("# hi", encoding="utf-8")
    wid = manager.create("项目", root_kind="external", root=str(ext))
    result = manager.detail(wid)
    assert result["error"] is None
    paths = {entry["path"] for entry in result["entries"]}
    assert {"src", "src/main.py", "README.md"} <= paths
    assert next(e for e in result["entries"] if e["path"] == "src")["dir"] is True
    assert next(e for e in result["entries"] if e["path"] == "README.md")["size"] == 4


def test_detail_respects_file_limit_and_marks_truncated(tmp_path):
    root, store, manager = _manager(tmp_path)
    settings = store.load("settings")
    settings.workspace.file_limit = 5
    settings.workspace.file_depth = 3
    store.save("settings", settings)
    ext = tmp_path / "many"
    ext.mkdir()
    for i in range(20):
        (ext / f"f{i}.txt").write_text("x", encoding="utf-8")
    wid = manager.create("多文件", root_kind="external", root=str(ext))
    result = manager.detail(wid)
    assert len(result["entries"]) == 5
    assert result["truncated"] is True


def test_detail_respects_file_depth(tmp_path):
    root, store, manager = _manager(tmp_path)
    settings = store.load("settings")
    settings.workspace.file_depth = 1
    store.save("settings", settings)
    ext = tmp_path / "deep"
    (ext / "a" / "b").mkdir(parents=True)
    wid = manager.create("深目录", root_kind="external", root=str(ext))
    paths = {entry["path"] for entry in manager.detail(wid)["entries"]}
    assert "a" in paths
    assert "a/b" not in paths  # 深度 1 = 只列一层


def test_detail_does_not_follow_symlink(tmp_path):
    """R9：不跟随符号链接/联接点 —— 防环、防越界遍历。无权限建链接时跳过。"""
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret.txt").write_text("不该出现", encoding="utf-8")
    try:
        os.symlink(target, ext / "link", target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("本机不允许创建符号链接（需开发者模式或管理员权限）")
    wid = manager.create("项目", root_kind="external", root=str(ext))
    paths = {entry["path"] for entry in manager.detail(wid)["entries"]}
    assert "link" not in paths
    assert "link/secret.txt" not in paths


def test_detail_rejects_escaping_subpath(tmp_path):
    _, _, manager = _manager(tmp_path)
    wid = manager.create("项目")
    for bad in ("..", "../..", str(tmp_path)):
        result = manager.detail(wid, bad)
        assert result["error"]
        assert result["entries"] == []


def test_detail_reports_missing_root(tmp_path):
    _, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    wid = manager.create("项目", root_kind="external", root=str(ext))
    ext.rmdir()
    result = manager.detail(wid)
    assert result["error"] and "不存在" in result["error"]


def test_detail_of_empty_workspace_has_own_skeleton(tmp_path):
    """托管工作区自带 `.ymtdata` 骨架 —— 列举应能看到它，而不是空列表。"""
    _, _, manager = _manager(tmp_path)
    wid = manager.create("空项目")
    names = {entry["name"] for entry in manager.detail(wid)["entries"]}
    assert ".ymtdata" in names


# -- 审计与接口 ---------------------------------------------------------------
def test_mutations_are_audited(tmp_path):
    root, _, manager = _manager(tmp_path)
    ext = tmp_path / "proj"
    ext.mkdir()
    wid = manager.create("项目", root_kind="external", root=str(ext))
    manager.switch(wid)
    manager.update(wid, name="改名")
    manager.delete(wid)
    actions = _audit_actions(root)
    assert actions == [
        "workspace.create",
        "workspace.switch",
        "workspace.update",
        "workspace.delete",
    ]


def test_audit_does_not_record_paths(tmp_path):
    """审计只记 id / 档位，不记路径 —— 持久化面越小越好（与 shell 不记命令原文同族）。"""
    root, _, manager = _manager(tmp_path)
    ext = tmp_path / "secret-location"
    ext.mkdir()
    manager.create("项目", root_kind="external", root=str(ext))
    raw = (root / "logs" / "audit.jsonl").read_text(encoding="utf-8")
    assert "secret-location" not in raw


def test_manager_is_instance_of_interface(tmp_path):
    """接口与实现不许漂移：漏实现抽象方法时这里会直接 TypeError。"""
    _, _, manager = _manager(tmp_path)
    assert isinstance(manager, IWorkspaceManager)
