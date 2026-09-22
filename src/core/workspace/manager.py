"""工作区宿主（v0.0.6 spec §3.3 / §3.13）。

职责：登记表读写（`ymtdata/workspaces/index.json`，唯一事实源）→ 默认工作区保障 →
CRUD → 当前工作区（**运行态，不落盘**）→ 路径解析与禁设校验 → 有界文件列举 → 审计。

设计约束：
- 依赖仅 `{shared, core.store}`（与 `core.shell` / `core.skills` 同层），**不得**依赖
  `core.agent`（会话计数由调用方传入，反向依赖就此掐断）。
- **工作区不是模块**：无 enabled 档、不进 `MODULE_NAMES`，不参与「模块健康徽标」语义。
- **不发事件**：本宿主不 import 事件信封；推送由 `CoreController` 统一决定（单一出口）。
- 删除只摘登记，**绝不删磁盘**（spec §3.13 R2）；`ws_default` 不可删不可搬（R3）。
- 名称不参与路径构造（R4），路径编码一律经 `layout.py` 解析（禁止字符串拼接）。
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shared.ids import WS, WS_DEFAULT, new_id
from shared.schema import WorkspaceIndex, WorkspaceRecord, WorkspaceSettings

from core.store.atomic import atomic_write_json, atomic_write_text

from core.workspace import layout
from core.workspace.layout import WorkspaceDenied, WorkspacePathError

log = logging.getLogger(__name__)

#: 默认工作区的展示名（用户可改；id 恒为 `ws_default`）。
DEFAULT_NAME = "默认工作区"

#: 默认工作区在数据根内的相对路径 —— 与 v0.0.5 及以前**完全一致**（存量数据零迁移）。
DEFAULT_ROOT = "workspace"

#: 名称长度上限（纯展示，防界面被超长文本撑坏）。
MAX_NAME = 60

#: 单次创建可写入的构建命令长度上限（有界成本）。
MAX_BUILD_CMD = 2000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IWorkspaceManager(ABC):
    """宿主对外契约（controller 只依赖本接口声明的方法）。

    形状与 `IShellManager` / `IMcpManager` 同族：声明即契约，替换实现（含测试替身）
    必须能实例化；`tests/static/test_contract_coverage.py` 会把「controller 调了但没声明」
    当场拦下。
    """

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def list_status(self, counts: dict[str, int] | None = None) -> list[dict]: ...

    @abstractmethod
    def collapsed(self) -> list[str]: ...

    @abstractmethod
    def current(self) -> str: ...

    @abstractmethod
    def exists(self, workspace_id: str) -> bool: ...

    @abstractmethod
    def switch(self, workspace_id: str) -> None: ...

    @abstractmethod
    def create(
        self,
        name: str,
        root_kind: str = "managed",
        root: str | None = None,
        data_home_kind: str | None = None,
        data_home: str | None = None,
        build_cmd: str = "",
        note: str | None = None,
    ) -> str: ...

    @abstractmethod
    def update(
        self,
        workspace_id: str,
        *,
        name: str,
        note: str = "",
        build_cmd: str = "",
        root: str | None = None,
        data_home_kind: str | None = None,
        data_home: str | None = None,
    ) -> None: ...

    @abstractmethod
    def delete(self, workspace_id: str) -> None: ...

    @abstractmethod
    def detail(self, workspace_id: str, path: str = "") -> dict: ...

    @abstractmethod
    def collapse(self, workspace_id: str, collapsed: bool) -> list[str]: ...

    @abstractmethod
    def last_error(self) -> str: ...

    @abstractmethod
    def shutdown(self) -> None: ...


class WorkspaceManager(IWorkspaceManager):
    def __init__(
        self,
        root: Path,
        config_store: Any = None,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self._data_root = Path(root)
        self._config_store = config_store
        self._audit = audit or (lambda *a, **k: None)
        self._index: WorkspaceIndex = WorkspaceIndex()
        self._current = WS_DEFAULT
        self._error = ""

    # ---------- 登记表 ----------
    @property
    def index_path(self) -> Path:
        return self._data_root / "workspaces" / "index.json"

    def _settings(self) -> WorkspaceSettings:
        try:
            return self._config_store.load("settings").workspace
        except Exception:  # noqa: BLE001 - 配置异常回退出厂默认，不阻断工作区能力
            log.exception("工作区配置读取失败，回退默认")
            return WorkspaceSettings()

    def _default_record(self) -> WorkspaceRecord:
        """默认工作区：root 与 data_home **重合**于 `ymtdata/workspace/`（存量形状不变）。"""
        return WorkspaceRecord(
            id=WS_DEFAULT,
            name=DEFAULT_NAME,
            root_kind="managed",
            root=DEFAULT_ROOT,
            data_home_kind="managed",
            data_home=DEFAULT_ROOT,
            created_at=_utcnow(),
            note="应用内置：不以任何工作区为目标时的落点与记忆共享层，不可删除。",
        )

    def load(self) -> None:
        """读登记表；文件缺失或为空则生成默认工作区（幂等）。"""
        self._error = ""
        records = self._read_index()
        if not any(r.id == WS_DEFAULT for r in records):
            records = [self._default_record(), *records]
            self._index = WorkspaceIndex(workspaces=records)
            self._write_index()  # 缺默认工作区（或整表缺失）时立刻补写
        else:
            self._index = WorkspaceIndex(workspaces=records)
        if self._current not in {r.id for r in records}:
            self._current = WS_DEFAULT
        self._ensure_dirs(self._record(WS_DEFAULT))

    def _read_index(self) -> list[WorkspaceRecord]:
        path = self.index_path
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("工作区登记表不可读，按空表重建（原文件保留）：%s", path)
            self._error = "工作区登记表不可读，已按空表加载。"
            return []
        try:
            return list(WorkspaceIndex.model_validate(data).workspaces)
        except Exception as exc:  # noqa: BLE001 - 手编坏文件不应让应用起不来
            log.warning("工作区登记表格式不合法：%s", type(exc).__name__)
            self._error = "工作区登记表格式不合法，已按空表加载（请检查 workspaces/index.json）。"
            return []

    def _write_index(self) -> None:
        atomic_write_json(self.index_path, self._index.model_dump(mode="json"))

    # ---------- 解析 ----------
    def _record(self, workspace_id: str) -> WorkspaceRecord:
        for record in self._index.workspaces:
            if record.id == workspace_id:
                return record
        raise KeyError(f"workspace_not_found: {workspace_id}")

    def root_of(self, workspace_id: str | None) -> Path:
        record = self._record(workspace_id or WS_DEFAULT)
        return layout.resolve_root(record.root_kind, record.root, self._data_root)

    def data_home_of(self, workspace_id: str | None) -> Path:
        record = self._record(workspace_id or WS_DEFAULT)
        root = layout.resolve_root(record.root_kind, record.root, self._data_root)
        return layout.resolve_data_home(
            record.data_home_kind, record.data_home, root, self._data_root
        )

    def cascade_dirs(self, workspace_id: str | None) -> list[Path]:
        """记忆级联的工作区层：**默认工作区 → 当前工作区**，按物理路径去重并保序。

        `active == default` 时两层是同一文件 → 去重后只注入一次
        （否则同一段记忆重复占用 token，且违反「一般→具体」的意图，spec §3.5.2）。
        """
        return layout.unique_paths([self.data_home_of(None), self.data_home_of(workspace_id)])

    def _ensure_dirs(self, record: WorkspaceRecord) -> None:
        """确保 root / data_home / documents / artifacts / manifest / AGENTS.md 存在（幂等）。

        **外部 root 已消失时一律早退**：不重建、不写数据落点。
        否则「用户手动删/移走了外部目录」会被一次改名悄悄修复成一个空目录，
        `missing` 信号随之消失 —— 用户就再也看不出自己的文件去哪了（该状态只能由用户决定怎么处理）。
        托管 root 相反：那是宿主分配的位置，重建是正确的。
        """
        root = layout.resolve_root(record.root_kind, record.root, self._data_root)
        if record.root_kind != "managed" and not root.is_dir():
            log.info("外部工作区目录不存在，跳过骨架补建：%s", record.id)
            return
        data_home = layout.resolve_data_home(
            record.data_home_kind, record.data_home, root, self._data_root
        )
        root.mkdir(parents=True, exist_ok=True)
        for sub in (data_home, data_home / "documents", data_home / "artifacts"):
            sub.mkdir(parents=True, exist_ok=True)
        manifest = data_home / "manifest.json"
        if not manifest.exists():
            atomic_write_json(manifest, {"schema_version": 1, "files": []})
        agents = data_home / "AGENTS.md"
        if not agents.exists():
            atomic_write_text(agents, "")

    # ---------- 查询 ----------
    def current(self) -> str:
        return self._current

    def exists(self, workspace_id: str) -> bool:
        return any(r.id == workspace_id for r in self._index.workspaces)

    def switch(self, workspace_id: str) -> None:
        self._record(workspace_id)  # 不存在即 KeyError（调用方给可读回报）
        self._current = workspace_id
        self._audit("workspace.switch", id=workspace_id)

    def list_status(self, counts: dict[str, int] | None = None) -> list[dict]:
        """工作区列表（展示视图）：路径已解析为物理绝对路径，派生徽标在宿主侧算定。"""
        tally = counts or {}
        out: list[dict] = []
        for record in self._index.workspaces:
            try:
                root = layout.resolve_root(record.root_kind, record.root, self._data_root)
                data_home = layout.resolve_data_home(
                    record.data_home_kind, record.data_home, root, self._data_root
                )
            except WorkspacePathError:
                # 手编出非法路径编码：**仍然列出这一条**（标成 missing 且路径留空），
                # 而不是让工作区从列表里静默消失 —— 消失了用户就无从知道要改哪一行。
                log.warning("工作区路径编码非法，按缺失展示：%s", record.id)
                out.append(
                    {
                        "id": record.id,
                        "name": record.name,
                        "builtin": record.id == WS_DEFAULT,
                        "current": record.id == self._current,
                        "root_kind": record.root_kind,
                        "data_home_kind": record.data_home_kind,
                        "root": "",
                        "data_home": "",
                        "migratable": False,
                        "missing": True,
                        "sessions": int(tally.get(record.id, 0)),
                        "build_cmd": record.build_cmd or "",
                        "note": record.note,
                    }
                )
                continue
            out.append(
                {
                    "id": record.id,
                    "name": record.name,
                    "builtin": record.id == WS_DEFAULT,
                    "current": record.id == self._current,
                    "root_kind": record.root_kind,
                    "data_home_kind": record.data_home_kind,
                    "root": str(root),
                    "data_home": str(data_home),
                    "migratable": _is_migratable(root, data_home, self._data_root),
                    "missing": not root.is_dir(),
                    "sessions": int(tally.get(record.id, 0)),
                    "build_cmd": record.build_cmd or "",
                    "note": record.note,
                }
            )
        return out

    def collapsed(self) -> list[str]:
        """侧栏折叠态（UI 偏好，落 `settings.ui.collapsed_workspaces`）。"""
        try:
            return list(self._config_store.load("settings").ui.collapsed_workspaces)
        except Exception:  # noqa: BLE001 - 折叠态读取失败不影响功能
            return []

    def collapse(self, workspace_id: str, collapsed: bool) -> list[str]:
        """写入折叠态并返回新列表。

        **两条路径并存，别混**：界面点按走既有 `settings.update(section="ui")`
        （不新增请求类型的代价就是这条路径），本方法是宿主侧 API，
        目前由 `delete()` 用来清理消失工作区的残留 id。
        """
        ids = self.collapsed()
        if collapsed and workspace_id not in ids:
            ids.append(workspace_id)
        if not collapsed:
            ids = [i for i in ids if i != workspace_id]
        if self._config_store is not None:
            settings = self._config_store.load("settings")
            settings.ui.collapsed_workspaces = ids
            self._config_store.save("settings", settings)
        return ids

    # ---------- 写 ----------
    def _clean_name(self, name: str) -> str:
        text = " ".join(str(name or "").split())
        if not text:
            raise WorkspacePathError("工作区名称不能为空。")
        return text[:MAX_NAME]

    def _check_root_candidate(self, candidate: Path) -> None:
        reason = layout.forbid_reason(candidate, self._data_root)
        if reason:
            raise WorkspaceDenied(reason)  # 策略拒绝（R5），归 `denied` 而非 `invalid_request`
        if not candidate.is_dir():
            raise WorkspacePathError(f"目录不存在或不是文件夹：{candidate}")

    def _assert_root_free(self, candidate: Path, exclude_id: str = "") -> None:
        """禁止两个工作区指向同一目录（否则记忆与文件视图会互相串）。"""
        for record in self._index.workspaces:
            if record.id == exclude_id:
                continue
            try:
                other = layout.resolve_root(record.root_kind, record.root, self._data_root)
            except WorkspacePathError:
                continue
            if layout.same_path(candidate, other):
                raise WorkspacePathError(
                    f"该目录已被工作区「{record.name}」使用，请换一个目录。"
                )

    def create(
        self,
        name: str,
        root_kind: str = "managed",
        root: str | None = None,
        data_home_kind: str | None = None,
        data_home: str | None = None,
        build_cmd: str = "",
        note: str | None = None,
    ) -> str:
        clean = self._clean_name(name)
        kind = root_kind if root_kind in ("managed", "external") else "managed"
        workspace_id = new_id(WS)
        if kind == "external":
            if not str(root or "").strip():
                raise WorkspacePathError("外部工作区必须选择一个目录。")
            candidate = layout.abs_path(root)
            # 托管路径由宿主自己分配，不做禁设校验（它本就在数据根的托管区内）
            self._check_root_candidate(candidate)
        else:
            candidate = layout.abs_path(self._data_root / layout.MANAGED_AREA / workspace_id)
        self._assert_root_free(candidate)

        home_kind = data_home_kind or self._settings().default_data_home_kind
        if home_kind not in ("inline", "managed", "custom"):
            home_kind = "inline"
        if home_kind == "inline":
            home_value = layout.DATA_HOME_DIRNAME
        elif home_kind == "managed":
            home_value = f"{layout.MANAGED_AREA}/{workspace_id}/data"
        else:
            if not str(data_home or "").strip():
                raise WorkspacePathError("自定义数据落点必须指定目录。")
            home_value = str(layout.abs_path(data_home))

        record = WorkspaceRecord(
            id=workspace_id,
            name=clean,
            root_kind=kind,
            root=str(candidate) if kind == "external" else f"{layout.MANAGED_AREA}/{workspace_id}",
            data_home_kind=home_kind,
            data_home=home_value,
            created_at=_utcnow(),
            build_cmd=(build_cmd or "")[:MAX_BUILD_CMD] or None,
            note=(note or None),
        )
        self._ensure_dirs(record)
        self._index.workspaces.append(record)
        self._write_index()
        self._audit("workspace.create", id=workspace_id, root_kind=kind, data_home_kind=home_kind)
        return workspace_id

    def update(
        self,
        workspace_id: str,
        *,
        name: str,
        note: str = "",
        build_cmd: str = "",
        root: str | None = None,
        data_home_kind: str | None = None,
        data_home: str | None = None,
    ) -> None:
        record = self._record(workspace_id)
        builtin = record.id == WS_DEFAULT
        record.name = self._clean_name(name)
        record.note = note.strip() or None
        record.build_cmd = (build_cmd or "")[:MAX_BUILD_CMD] or None

        changed: list[str] = []
        if root is not None and not builtin:
            candidate = layout.abs_path(root)
            self._check_root_candidate(candidate)
            self._assert_root_free(candidate, exclude_id=workspace_id)
            record.root_kind = "external"
            record.root = str(candidate)
            changed.append("root")
        elif root is not None and builtin:
            raise WorkspaceDenied("默认工作区不可更换目录（它是应用内置的落点）。")

        if data_home_kind is not None:
            if builtin:
                raise WorkspaceDenied("默认工作区不可更换数据落点。")
            kind = data_home_kind
            if kind not in ("inline", "managed", "custom"):
                raise WorkspacePathError(f"未知的数据落点：{data_home_kind!r}")
            if kind == "inline":
                value = layout.DATA_HOME_DIRNAME
            elif kind == "managed":
                value = f"{layout.MANAGED_AREA}/{workspace_id}/data"
            else:
                if not str(data_home or "").strip():
                    raise WorkspacePathError("自定义数据落点必须指定目录。")
                value = str(layout.abs_path(data_home))
            if kind != record.data_home_kind or value != record.data_home:
                record.data_home_kind = kind
                record.data_home = value
                changed.append("data_home")

        self._ensure_dirs(record)  # 更换目录/落点后补建骨架（不迁移既有数据，见 docstring）
        self._write_index()
        self._audit("workspace.update", id=workspace_id, changed=",".join(changed) or "meta")

    def delete(self, workspace_id: str) -> None:
        """移除登记。**只摘登记、不删磁盘**（R2）：真实目录与 `.ymtdata/` 一律保留。

        `ws_default` 拒绝（R3）—— 应用必须永远有一个可用的落点。
        """
        record = self._record(workspace_id)
        if record.id == WS_DEFAULT:
            raise WorkspaceDenied("默认工作区不可移除。")
        self._index.workspaces = [r for r in self._index.workspaces if r.id != workspace_id]
        if self._current == workspace_id:
            self._current = WS_DEFAULT
        self._write_index()
        # 顺带清掉折叠态里的残留 id（否则 settings 会越积越脏）
        ids = self.collapsed()
        if workspace_id in ids:
            self.collapse(workspace_id, False)
        self._audit("workspace.delete", id=workspace_id)

    # ---------- 文件列举 ----------
    def detail(self, workspace_id: str, path: str = "") -> dict:
        """**有界**递归列举（深度 `file_depth`、条数 `file_limit`），不跟随符号链接（R9）。

        返回 `{id, path, root, data_home, entries, truncated, error}`；
        `entries` 每项为 `{path, name, dir, size}`（`path` 相对 root，供界面还原层级）。
        """
        record = self._record(workspace_id)
        root = layout.resolve_root(record.root_kind, record.root, self._data_root)
        data_home = layout.resolve_data_home(
            record.data_home_kind, record.data_home, root, self._data_root
        )
        settings = self._settings()
        max_depth = max(1, int(settings.file_depth))
        limit = max(1, int(settings.file_limit))
        result: dict[str, Any] = {
            "id": workspace_id,
            "path": path or "",
            "root": str(root),
            "data_home": str(data_home),
            "entries": [],
            "truncated": False,
            "error": None,
        }
        try:
            base = layout.safe_child(root, path)
        except WorkspacePathError as exc:
            result["error"] = str(exc)
            return result
        if not root.is_dir():
            result["error"] = f"目录不存在（可能已被移动或删除）：{root}"
            return result
        if not base.is_dir():
            result["error"] = f"子路径不存在或不是文件夹：{path}"
            return result

        entries: list[dict] = []
        frontier: list[tuple[Path, int]] = [(base, 0)]
        truncated = False
        while frontier and not truncated:
            current, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            try:
                children = sorted(
                    current.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())
                )
            except OSError:
                continue  # 无权限目录跳过，不让一次列举整体失败
            for child in children:
                if len(entries) >= limit:
                    truncated = True
                    break
                if _is_link_like(child):
                    continue  # R9：不跟随符号链接/联接点（防环、防越界）
                try:
                    is_dir = child.is_dir()
                    size = 0 if is_dir else child.stat().st_size
                except OSError:
                    continue
                entries.append(
                    {
                        "path": _rel_of(child, root),
                        "name": child.name,
                        "dir": is_dir,
                        "size": int(size),
                    }
                )
                if is_dir:
                    frontier.append((child, depth + 1))
        result["entries"] = entries
        result["truncated"] = truncated
        return result

    # ---------- 状态 ----------
    def last_error(self) -> str:
        return self._error

    def shutdown(self) -> None:
        """无长效资源（工作区不持进程/句柄）；保留接口与其它宿主同形。"""


def _is_migratable(root: Path, data_home: Path, data_root: Path) -> bool:
    """「拷贝数据根即完成迁移」对本工作区是否成立。

    判据是**解析后的物理路径**，不是档位标签 —— 早前按
    `root_kind == "managed" and data_home_kind == "managed"` 判，会把
    「托管目录 + `inline` 落点」（两者都在数据根内，明明可迁移）误标为不可迁移。
    只要 root 与 data_home 都落在数据根之内，搬到别处就是完整的。
    """
    return layout.is_under(root, data_root) and layout.is_under(data_home, data_root)


def _is_link_like(path: Path) -> bool:
    """符号链接或 Windows 目录联接点（junction）—— 两者都不跟随。"""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            return bool(is_junction())
        except OSError:
            return True
    return False


def _rel_of(child: Path, root: Path) -> str:
    try:
        return os.path.relpath(str(child), str(root)).replace(os.sep, "/")
    except ValueError:  # 跨盘符（理论上不会发生：child 必为 root 之下）
        return child.name


__all__ = [
    "DEFAULT_NAME",
    "DEFAULT_ROOT",
    "IWorkspaceManager",
    "MAX_BUILD_CMD",
    "MAX_NAME",
    "WorkspaceManager",
    "WorkspacePathError",
]
