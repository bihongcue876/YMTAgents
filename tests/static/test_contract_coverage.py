"""接口契约覆盖检查（门禁）。

背景：`IModelGateway` 曾漏声明 `reload_settings` / `test_connection` /
`upsert_provider` / `delete_provider`，而 `CoreController` 直接调用它们 ——
任何按接口实现的替身（如 `tests/mocks/gateway.py`）必然 `AttributeError`，
且这些路径长期无测试覆盖。

本检查把「controller 依赖的协作方方法必须在其接口中声明」固化为构建门禁，
新增协作调用却忘记回写接口时会直接失败（docs 04 §1 依赖规则的同族约束）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
CONTROLLER = SRC / "app" / "controller.py"

# controller 属性名 → 该属性应满足的接口（None = 具体类，不检查）
COLLABORATORS: dict[str, tuple[str, str] | None] = {
    "gateway": ("core.gateway.provider", "IModelGateway"),
    "store": ("core.agent.session", "ISessionStore"),
    "agent": ("core.agent.loop", "IAgentLoop"),
    "supervisor": ("core.modules.supervisor", "IModuleSupervisor"),
    "features": ("core.modules.feature", "IFeatureManager"),
    "library_manager": ("core.library.manager", "ILibraryService"),
    "shell_manager": ("core.shell.manager", "IShellManager"),
    "workspace_manager": ("core.workspace.manager", "IWorkspaceManager"),
    "registry": None,
    "config_store": None,
    "bridge": None,
}


def _called_methods(attr: str) -> set[str]:
    """收集 `self.<attr>.<method>` 形式访问的方法名。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(CONTROLLER.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute):
            continue
        owner = node.value
        if isinstance(owner.value, ast.Name) and owner.value.id == "self" and owner.attr == attr:
            names.add(node.attr)
    return names


def test_controller_collaborator_calls_are_declared():
    import importlib

    violations: list[str] = []
    for attr, target in COLLABORATORS.items():
        if target is None:
            continue
        module_name, class_name = target
        interface = getattr(importlib.import_module(module_name), class_name)
        for method in sorted(_called_methods(attr)):
            if not hasattr(interface, method):
                violations.append(f"self.{attr}.{method}() 未在 {class_name} 中声明")
    assert not violations, "controller 依赖了未声明的接口方法：\n" + "\n".join(violations)


def test_mock_gateway_satisfies_interface():
    """替身必须能实例化 —— 即实现全部抽象方法（缺一个即 TypeError）。"""
    from core.gateway.provider import IModelGateway
    from tests.mocks.gateway import MockGateway

    assert isinstance(MockGateway(), IModelGateway)


def test_dpim_contracts_are_in_discriminated_unions():
    from shared.envelope import EVENT_MODELS, REQUEST_MODELS

    requests = {model.__name__ for model in REQUEST_MODELS}
    events = {model.__name__ for model in EVENT_MODELS}
    assert {"LibraryCreate", "LibraryUpdate", "LibraryDelete", "LibrarySwitch",
            "LibraryRefresh", "LibraryDetail", "LibraryIngest", "LibraryQuery"} <= requests
    assert {"LibraryList", "LibraryDetailResult", "LibraryIngestResult",
            "LibraryQueryResult", "LibraryGraphResult"} <= events
    assert (len(REQUEST_MODELS), len(EVENT_MODELS)) == (67, 46)


def test_workspace_slice_contracts_are_in_discriminated_unions():
    """工作区后续切片（shell cwd 绑定 / 记忆级联 / 附件与构建 / 文件编辑）的契约在联合中。"""
    from shared.envelope import EVENT_MODELS, REQUEST_MODELS

    requests = {model.__name__ for model in REQUEST_MODELS}
    events = {model.__name__ for model in EVENT_MODELS}
    assert {"WorkspaceMemoryWrite", "WorkspaceBuild", "WorkspaceFileRead",
            "WorkspaceFileWrite"} <= requests
    assert {"WorkspaceMemoryResult", "WorkspaceBuildResult", "WorkspaceFileResult"} <= events
