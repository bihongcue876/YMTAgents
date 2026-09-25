"""文件访问层的路径策略与禁止区（spec-2026-09-24-file-tools §2.2 / §11.3 D-5）。

性质：**纯函数 + 零 IO**（与 core.workspace.layout 同族）——同一输入必得同一输出。
判定一律基于**规范化绝对路径**（abspath：Windows 大小写不敏感、解析 ..、不查符号链接），
且判定在**核心侧**，GUI 不参与。

单一来源（D-5）：
- 本模块的 FILENAME_DENY / DIRNAME_DENY / PRIVATE_UNDER_DATA_ROOT 是**路径**层排除的唯一来源；
- shared.redact 只管**内容**层（密钥形态）——两者分工明确、各自单源，不互相引用。
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path

#: inline 数据落点的目录名（与 core.workspace.layout 同值；此处为单一来源）。
DATA_HOME_DIRNAME = ".ymtdata"

#: 文件名硬排除：读 / 查 / 写三处一致，**任何开关都不放开**（含 include_hidden）。
FILENAME_DENY: tuple[str, ...] = ("*.pem", "*.key", "id_rsa*", ".env*", "vault*", "*.pfx")

#: 目录名硬排除（全禁；遍历时直接剪枝）：VCS 元数据与工作区私有数据目录。
DIRNAME_DENY: tuple[str, ...] = (".git", DATA_HOME_DIRNAME)

#: 数据根下由各管理面**独占写**的资产：读允许、写拒绝，遍历默认跳过。
PRIVATE_UNDER_DATA_ROOT: tuple[str, ...] = (
    "config", "workspaces", "libraries", "sessions", "secrets",
    "logs", "backup", "skills", "personas",
)

_LOWER_DIRNAME_DENY = tuple(name.lower() for name in DIRNAME_DENY)

#: Windows 保留设备名（去扩展名后判定；任意扩展名形态均算，安全修订轮）：
#: 写向设备会落空、读设备可能挂死，一律按不可接受路径处理。
_RESERVED_STEMS: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul",
     *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def is_reserved_name(name: str) -> bool:
    """文件名主干（去扩展名）是否为 Windows 保留设备名。"""
    return name.lower().split(".", 1)[0] in _RESERVED_STEMS


class FilePathError(ValueError):
    """路径不可接受（编码非法 / 越界 / 非普通文件）——归 invalid_request。"""


class FileDenied(FilePathError):
    """**策略拒绝**（禁止区 / 区外写入 / 文件名硬排除）——归 tool_denied（不打扰用户）。"""


class FileMissing(FilePathError):
    """目标不存在（读侧）——归 file_not_found。"""


class FileNoMatch(FilePathError):
    """old_string 未命中——归 edit_no_match。"""


class FileAmbiguous(FilePathError):
    """old_string 命中多处——归 edit_ambiguous。"""


def abs_path(path: str | Path) -> Path:
    """规范化绝对路径（**不查文件系统**：不解析符号链接、strict=False）。"""
    return Path(os.path.abspath(str(path)))


def path_key(path: str | Path) -> str:
    """路径同一性键（Windows 大小写不敏感 + 统一分隔符）。"""
    return os.path.normcase(os.path.abspath(str(path)))


def same_path(a: str | Path, b: str | Path) -> bool:
    """两个路径是否指向同一位置（大小写不敏感）。"""
    return path_key(a) == path_key(b)


def is_under(child: str | Path, parent: str | Path) -> bool:
    """child 是否在 parent 之内（含自身）；大小写不敏感（Windows 语义）。"""
    child_key = path_key(child)
    parent_key = path_key(parent)
    if child_key == parent_key:
        return True
    return child_key.startswith(parent_key.rstrip("\\/") + os.sep)


def is_hidden_name(name: str) -> bool:
    """隐藏项（以点开头）；硬排除模式不受此影响。"""
    return name.startswith(".")


def is_denied_name(name: str) -> bool:
    """文件名是否命中硬排除模式（大小写不敏感）。"""
    low = name.lower()
    return any(fnmatch.fnmatch(low, pattern) for pattern in FILENAME_DENY)


def is_denied_dir(name: str) -> bool:
    """目录名是否命中硬排除（遍历剪枝用）。"""
    return name.lower() in _LOWER_DIRNAME_DENY


def is_private_dir(path: str | Path, data_root: str | Path) -> bool:
    """数据根下由管理面独占的目录（遍历剪枝：不搜宿主私有资产）。"""
    target = abs_path(path)
    root = abs_path(data_root)
    if not is_under(target, root):
        return False
    rel = target.relative_to(root)
    if not rel.parts:
        return False
    return rel.parts[0].lower() in {name.lower() for name in PRIVATE_UNDER_DATA_ROOT}


@dataclass(frozen=True)
class Access:
    """一次访问判定的结果：读 / 写 / 遍历三维度 + 理由。"""

    path: Path
    inside_workspace: bool
    read: bool
    write: bool
    search: bool
    reason: str = ""


def classify(path: str | Path, *, workspace_root: str | Path, data_root: str | Path) -> Access:
    """按位置给出有效判定（spec §2.1 权限矩阵）。

    - 工作区内：读 / 写 / 遍历皆放行（写仍需关卡，由执行器按声明档决定）；
    - 工作区外：读放行但需审核（D1），**写一律拒绝**（D2），不做默认遍历；
    - 禁止区：读写皆拒（机密库 / VCS 元数据 / 工作区私有数据 / 文件名硬排除）；
    - 数据根下宿主私有资产：读允许、写拒绝（各管理面独占写）。
    """
    target = abs_path(path)
    root = abs_path(workspace_root)
    data = abs_path(data_root)
    inside = is_under(target, root)

    if is_denied_name(target.name):
        return Access(target, inside, False, False, False, "文件名命中硬排除模式。")
    if is_under(target, data / "secrets"):
        return Access(target, inside, False, False, False, "该位置属于本机机密库，禁止访问。")
    for part in target.parts:
        if part.lower() in _LOWER_DIRNAME_DENY:
            return Access(target, inside, False, False, False, "该位置属于禁止目录（VCS 元数据或私有数据）。")

    if inside:
        return Access(target, True, True, True, True)
    if is_under(target, data):
        return Access(target, False, True, False, False, "该位置属于应用数据目录（宿主私有资产），只读。")
    return Access(target, False, True, False, False, "该位置在工作区之外。")


def needs_review(access: Access) -> bool:
    """区外读取 = 允许但需用户审核（D1）；区内读取不打扰（D3）。"""
    return access.read and not access.inside_workspace


def reject_symlinks(target: str | Path) -> None:
    """逐段拒绝符号链接（含中间目录）与 junction/reparse 点；只判**已存在**的层级。

    junction 不被「is_symlink」识别（Windows 目录挂接点），但同样能把路径引到
    判定区之外——classify 基于**词典路径**比较，链接必须在判定的入口处拒掉（安全修订轮）。
    """
    path = abs_path(target)
    parts = path.parts
    if not parts:
        return
    current = Path(parts[0])
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for part in parts[1:]:
        current = current / part
        try:
            st = os.lstat(str(current))
        except FileNotFoundError:
            continue  # 该段尚不存在：不存在即不是链接，交由 must_exist 收口
        except NotADirectoryError:
            continue  # 中途已是文件：交由后续操作自然失败，不算链接
        except OSError:  # 权限不足等：fail-closed 反向处理，不因探测失败而放行
            raise FileDenied("无法确认该路径是否为符号链接，拒绝访问。")
        if stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0) & reparse_mask):
            raise FileDenied("路径不得包含符号链接或 junction。")


def resolve_target(raw: str | Path, *, root: str | Path, base: str | Path | None = None,
                   must_exist: bool = False) -> Path:
    """把入参路径解析为绝对路径：相对路径以 base（缺省 = 工作区 root）为锚。

    接受绝对路径（区外由 classify 判定）；拒绝空值、逐段拒绝符号链接；
    must_exist=True 时要求已存在的普通文件（归 file_not_found）。
    """
    text = str(raw or "").strip()
    if not text:
        raise FilePathError("路径不能为空。")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        target = abs_path(candidate)
    else:
        anchor = base if base is not None else root
        target = abs_path(Path(anchor) / text)
    # 保留设备名两段查（安全修订轮）：abspath 会把「…\NUL」规范化成设备路径
    # 「\\.\NUL」，target.name 变空串——必须在原始 candidate.name 上先拦一道。
    if is_reserved_name(candidate.name) or is_reserved_name(target.name):
        raise FilePathError("目标名是 Windows 保留设备名，拒绝访问。")
    reject_symlinks(target)
    if must_exist and not target.is_file():
        raise FileMissing("目标不是已存在的普通文件。")
    return target


def relative_display(path: str | Path, base: str | Path) -> str:
    """界面 / 审计用的显示路径：在 base 内给相对路径，否则给绝对路径。"""
    target = abs_path(path)
    anchor = abs_path(base)
    if is_under(target, anchor):
        try:
            return target.relative_to(anchor).as_posix() or "."
        except ValueError:
            return str(target)
    return str(target)
