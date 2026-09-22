"""工作区路径解析与禁设校验（v0.0.6 spec §3.3 / §3.13）。

性质：**纯函数 + 零 IO**（与 `core.shell.policy` 同族）——
同一输入必得同一输出，可数据驱动测试。文件系统存在性检查留在 `manager.py`：
本模块只管「路径怎么算」「哪些位置不许设」。

三个概念（spec §3.1）：

| 概念 | 解析规则 |
|---|---|
| `root`（干活目录） | `managed` → 相对数据根；`external` → 绝对路径 |
| `data_home`（记忆/文档落点） | `inline` → `<root>/.ymtdata`；`managed` → 相对数据根；`custom` → 绝对路径 |

`name` **永不参与路径构造**（docs 09 B5 同族的注入面收敛）：路径只由 id 与上述编码决定。
"""

from __future__ import annotations

import os
from pathlib import Path

#: `inline` 数据落点的目录名（用户裁决 2026-09-22 D3：默认在工作区之中建此目录）。
DATA_HOME_DIRNAME = ".ymtdata"

#: 托管工作区的分配区：`<数据根>/workspaces/<ws_id>/`。
#: 禁设校验要**放行**此区（宿主自己在此分配），而拒绝数据根下的其它位置。
MANAGED_AREA = "workspaces"


class WorkspacePathError(ValueError):
    """路径不可接受（编码非法 / 逃逸数据根 / 目录不存在 / 撞车）。"""


class WorkspaceDenied(WorkspacePathError):
    """**策略拒绝**（不是用户输入错误）：禁设清单命中、默认工作区不可动。

    与 `WorkspacePathError` 分家的唯一目的是**按真实原因归码**（项目铁律：错误码一码一义）：
    前者归 `invalid_request`（格式不合法），本类归 `denied`（策略不许），
    使界面能给出「为什么不行」而不是笼统的「格式不合法」。
    """


def abs_path(path: str | Path) -> Path:
    """规范化绝对路径（**不查文件系统**：不解析符号链接、`strict=False`）。

    用 `abspath`（纯字符串拼接 + normpath）而非 `Path.resolve()` —— 后者会查询
    文件系统解析链接，使「纯函数」不再可独立测试，且对被删目录行为不稳定。
    """
    return Path(os.path.abspath(str(path)))


def path_key(path: str | Path) -> str:
    """路径同一性键（Windows 大小写不敏感 + 统一分隔符）。

    用途：记忆级联去重（`active == default` 时两层同一物理文件）、
    以及工作区 root 撞车检测。
    """
    return os.path.normcase(os.path.abspath(str(path)))


def same_path(a: str | Path, b: str | Path) -> bool:
    return path_key(a) == path_key(b)


def _safe_relative(value: str, what: str) -> str:
    """校验「相对数据根」的编码：非空、非绝对、不含 `..`（防逃逸数据根）。"""
    text = str(value or "").strip()
    if not text:
        raise WorkspacePathError(f"{what} 不能为空。")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise WorkspacePathError(f"{what} 必须是相对数据根的相对路径，不接受绝对路径。")
    if ".." in candidate.parts:
        raise WorkspacePathError(f"{what} 不得包含 ..（不允许逃出数据根）。")
    return text


def resolve_root(root_kind: str, root: str, data_root: str | Path) -> Path:
    """把登记的 root 编码解析为物理绝对路径。"""
    if root_kind == "external":
        text = str(root or "").strip()
        if not text:
            raise WorkspacePathError("外部工作区必须指定目录。")
        return abs_path(text)
    if root_kind != "managed":
        raise WorkspacePathError(f"未知的 root_kind：{root_kind!r}")
    return abs_path(Path(data_root) / _safe_relative(root, "托管工作区的 root"))


def resolve_data_home(
    data_home_kind: str, data_home: str, root: Path, data_root: str | Path
) -> Path:
    """把登记的 data_home 编码解析为物理绝对路径。"""
    if data_home_kind == "inline":
        # 固定值：相对 root；登记里的 data_home 只作记录，实际以常量解析（防手编漂移）
        return abs_path(Path(root) / DATA_HOME_DIRNAME)
    if data_home_kind == "managed":
        return abs_path(Path(data_root) / _safe_relative(data_home, "数据落点"))
    if data_home_kind == "custom":
        text = str(data_home or "").strip()
        if not text:
            raise WorkspacePathError("自定义数据落点必须指定目录。")
        return abs_path(text)
    raise WorkspacePathError(f"未知的 data_home_kind：{data_home_kind!r}")


def is_under(child: str | Path, parent: str | Path) -> bool:
    """`child` 是否在 `parent` 之内（含自身）。大小写不敏感（Windows 语义）。"""
    child_key = path_key(child)
    parent_key = path_key(parent)
    if child_key == parent_key:
        return True
    return child_key.startswith(parent_key.rstrip("\\/") + os.sep)


def forbid_reason(candidate: str | Path, data_root: str | Path) -> str | None:
    """禁设清单（spec §3.13 R5）：命中返回**可读理由**，否则 `None`。

    拒绝理由都是「会让 shell cwd 与文件视图指向危险或冲突位置」：
    磁盘根、用户主目录、应用数据根、数据根下的非托管区、工作区数据目录自身。
    """
    path = abs_path(candidate)
    home = abs_path(Path.home())
    root = abs_path(data_root)

    if path == Path(path.anchor):
        return "不能把磁盘根目录设为工作区（shell 会在整盘范围内执行命令）。"
    if same_path(path, home):
        return "不能把用户主目录直接设为工作区（范围过大，误操作代价不可控）。"
    if same_path(path, root):
        return "不能把应用数据根设为工作区（与配置、会话目录冲突）。"
    managed_area = root / MANAGED_AREA
    if is_under(path, root) and not is_under(path, managed_area):
        return "该位置在应用数据根内且不属于工作区托管区，可能与配置或会话冲突。"
    if path.name == DATA_HOME_DIRNAME:
        return "不能把工作区数据目录（.ymtdata）本身设为工作区（会与记忆、文档落点重叠）。"
    return None


def safe_child(root: str | Path, relative: str) -> Path:
    """把界面给的**相对子路径**安全地接到 root 下；逃逸或非法则抛 `WorkspacePathError`。

    拒绝绝对路径与 `..` —— 文件列表不应成为越界遍历的入口（spec §3.7.4）。
    """
    text = str(relative or "").strip().replace("\\", "/").strip("/")
    if not text or text == ".":
        return abs_path(root)
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive:
        raise WorkspacePathError("只接受相对工作区根目录的子路径。")
    if ".." in candidate.parts:
        raise WorkspacePathError("子路径不得包含 ..。")
    return abs_path(Path(root) / text)


def unique_paths(paths: list[Path]) -> list[Path]:
    """按物理路径去重**并保序**（记忆级联用：`active == default` 时两层的同一文件只留一次）。

    与 `docs/03` §7「级联允许缺层、禁止环」配套 —— 去重是「禁环」在数据层的落实。
    """
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = path_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result
