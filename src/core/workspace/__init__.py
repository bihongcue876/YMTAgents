"""工作区宿主（v0.0.6）。

本包只依赖 `shared` 与 `core.store`（与 `core.shell` / `core.skills` 同层，见 docs 04 §1 依赖方向）。

- `layout.py`：路径推导弹与禁设清单 —— **纯函数、零 IO**，可数据驱动测试；
- `manager.py`：`WorkspaceManager`（登记表、默认工作区、CRUD、当前工作区、
  有界文件列举、审计）。工作区**不是模块**，不进 `MODULE_NAMES`。
"""

from core.workspace.layout import (
    DATA_HOME_DIRNAME,
    WorkspaceDenied,
    WorkspacePathError,
    forbid_reason,
    path_key,
    resolve_data_home,
    resolve_root,
    safe_child,
    same_path,
    unique_paths,
)
from core.workspace.manager import (
    DEFAULT_NAME,
    DEFAULT_ROOT,
    IWorkspaceManager,
    WorkspaceManager,
)

__all__ = [
    "DATA_HOME_DIRNAME",
    "DEFAULT_NAME",
    "DEFAULT_ROOT",
    "IWorkspaceManager",
    "WorkspaceDenied",
    "WorkspaceManager",
    "WorkspacePathError",
    "forbid_reason",
    "path_key",
    "resolve_data_home",
    "resolve_root",
    "safe_child",
    "same_path",
    "unique_paths",
]
