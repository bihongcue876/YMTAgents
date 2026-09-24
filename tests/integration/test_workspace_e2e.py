"""工作区端到端（v0.0.6）：一律从 `controller.handle(Request)` 入口走（rev5 教训）。

为什么必须走入口：直接调 `ctx.workspace_manager.xxx()` 会**绕过分派与事件层** ——
「请求类型没接进 dispatch」「事件没推给界面」这类缺口就测不出来。
另一半同样重要（rev8 教训）：断言要落在**消费端真的收到了数据**，而不是只看到事件对象 ——
所以本文件多处直接读 `sessions/<id>/meta.json` 与 `workspaces/index.json` 落盘事实。
"""

from __future__ import annotations

import json
from pathlib import Path

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import (
    MoveSession,
    NewSession,
    ResumeSession,
    SendMessage,
    ShellSpawn,
    SettingsUpdate,
    WorkspaceCreate,
    WorkspaceDelete,
    WorkspaceDetail,
WorkspaceMemoryWrite,
    WorkspaceBuild,
    WorkspaceRefresh,
    WorkspaceSwitch,
    WorkspaceUpdate,
)
from shared.ids import WS_DEFAULT
from shared.schema import ModulesConfig, ShellConfig
from tests.mocks.gateway import MockGateway
from core.store.config_store import ConfigStore


def _boot(tmp_path, monkeypatch, gateway=None):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    gateway = gateway or MockGateway()
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda _store: gateway)
    events: list = []
    ctx.bridge.event_received.connect(events.append)
    return ctx, events


def _of(events, kind: str) -> list:
    return [e for e in events if getattr(e, "type", "") == kind]


def _last(events, kind: str):
    found = _of(events, kind)
    return found[-1] if found else None


def _meta_of(ctx, session_id: str) -> dict:
    return json.loads(
        (Path(ctx.root) / "sessions" / session_id / "meta.json").read_text(encoding="utf-8")
    )


# -- 创建 / 切换 ---------------------------------------------------------------
def test_create_external_workspace_flows_through_controller(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ext = tmp_path / "proj"
    ext.mkdir()

    ctx.controller.handle(WorkspaceCreate(name="我的项目", root_kind="external", root=str(ext)))

    listing = _last(events, "workspace.list")
    assert listing is not None
    created = next(w for w in listing.workspaces if w.name == "我的项目")
    assert created.root_kind == "external"
    assert created.missing is False
    # 新建即切为当前（用户刚建完，下一步几乎必然在其中干活）
    assert listing.current == created.id
    snap = _last(events, "workspace.detail.result")
    assert snap is None  # 创建不伴随详情事件
    assert (ext / ".ymtdata" / "AGENTS.md").exists()  # D3：落点在区内


def test_create_carries_build_cmd_and_note(tmp_path, monkeypatch):
    """对话框填的构建命令与备注必须一路落到登记表（否则是静默丢数据）。"""
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(
        WorkspaceCreate(name="项目", build_cmd="make all", note="实验目录")
    )
    workspace_id = _last(events, "workspace.list").current
    raw = json.loads((Path(ctx.root) / "workspaces" / "index.json").read_text(encoding="utf-8"))
    record = next(r for r in raw["workspaces"] if r["id"] == workspace_id)
    assert record["build_cmd"] == "make all"
    assert record["note"] == "实验目录"


def test_create_is_reported_with_readable_reason_on_forbidden_root(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(
        WorkspaceCreate(name="盘根", root_kind="external", root=Path(ctx.root).anchor)
    )
    err = _last(events, "error")
    assert err is not None
    assert err.code == "workspace_denied"  # 策略拒绝按真实原因归码
    assert err.message and "磁盘根" in err.message  # 面向用户的文案可读，不是裸码


def test_create_with_bad_name_reports_invalid_request(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(WorkspaceCreate(name="   "))
    err = _last(events, "error")
    assert err is not None
    assert err.code == "invalid_request"


def test_switch_unknown_workspace_reports_error(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(WorkspaceSwitch(id="ws_nope"))
    err = _last(events, "error")
    assert err is not None
    assert err.code == "invalid_request"


def test_refresh_rereads_hand_edited_index(tmp_path, monkeypatch):
    """文件即配置：手编 `workspaces/index.json` 后点刷新即生效（不重启）。"""
    ctx, events = _boot(tmp_path, monkeypatch)
    index = Path(ctx.root) / "workspaces" / "index.json"
    data = json.loads(index.read_text(encoding="utf-8"))
    data["workspaces"][0]["name"] = "手改的名字"
    index.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    ctx.controller.handle(WorkspaceRefresh())

    listing = _last(events, "workspace.list")
    assert listing.workspaces[0].name == "手改的名字"


# -- 会话归属（D2）------------------------------------------------------------
def test_new_session_lands_in_current_workspace(tmp_path, monkeypatch):
    """核心链路：新建工作区 → 切为当前 → 新会话必须落在它下面，且**落盘可验**。"""
    ctx, events = _boot(tmp_path, monkeypatch)
    ext = tmp_path / "proj"
    ext.mkdir()
    ctx.controller.handle(WorkspaceCreate(name="项目", root_kind="external", root=str(ext)))
    workspace_id = _last(events, "workspace.list").current

    ctx.controller.handle(NewSession(title="在项目里聊"))
    session_id = ctx.controller.current_session_id

    assert _meta_of(ctx, session_id)["workspace_id"] == workspace_id  # 消费端真的收到了
    index = _last(events, "session.index")
    assert next(s for s in index.sessions if s.id == session_id).workspace_id == workspace_id
    counts = {w.id: w.sessions for w in _last(events, "workspace.list").workspaces}
    assert counts[workspace_id] == 1


def test_new_session_on_default_workspace_records_none(tmp_path, monkeypatch):
    """默认工作区记 `None`（与存量会话同态），避免「ws_default / None」两套表示法。"""
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(NewSession(title="默认"))
    assert _meta_of(ctx, ctx.controller.current_session_id)["workspace_id"] is None


def test_new_session_can_target_workspace_explicitly(tmp_path, monkeypatch):
    """侧栏「在此新建对话」：显式指定优先于当前工作区。"""
    ctx, events = _boot(tmp_path, monkeypatch)
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    ctx.controller.handle(WorkspaceCreate(name="甲", root_kind="external", root=str(first)))
    id_a = _last(events, "workspace.list").current
    ctx.controller.handle(WorkspaceCreate(name="乙", root_kind="external", root=str(second)))
    id_b = _last(events, "workspace.list").current
    assert id_a != id_b

    ctx.controller.handle(NewSession(title="回到甲", workspace_id=id_a))
    assert _meta_of(ctx, ctx.controller.current_session_id)["workspace_id"] == id_a


def test_move_session_between_workspaces(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ext = tmp_path / "proj"
    ext.mkdir()
    ctx.controller.handle(WorkspaceCreate(name="项目", root_kind="external", root=str(ext)))
    workspace_id = _last(events, "workspace.list").current
    # 新建即切为当前 → 显式切回默认工作区，让会话先在默认工作区里诞生
    ctx.controller.handle(WorkspaceSwitch(id=WS_DEFAULT))
    ctx.controller.handle(NewSession(title="先放默认"))
    session_id = ctx.controller.current_session_id
    assert _meta_of(ctx, session_id)["workspace_id"] is None


def test_workspace_memory_write_scope_append_replace_and_redaction(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    external = tmp_path / "memory-project"
    external.mkdir()
    ctx.controller.handle(WorkspaceCreate(name="记忆项目", root_kind="external", root=str(external)))
    workspace_id = _last(events, "workspace.list").current
    target = external / ".ymtdata" / "AGENTS.md"

    ctx.controller.handle(
        WorkspaceMemoryWrite(scope="current", mode="append", text="Use the project conventions.")
    )
    assert "Use the project conventions." in target.read_text(encoding="utf-8")
    assert _last(events, "workspace.memory.result").ok is True

    ctx.controller.handle(
        WorkspaceMemoryWrite(
            scope="specific", workspace_id=workspace_id, mode="append",
            text="api_key=sk-abc123456789 must be redacted.",
        )
    )
    saved = target.read_text(encoding="utf-8")
    assert "sk-abc123456789" not in saved
    assert "Use the project conventions." in saved
    assert target.with_suffix(".md.bak").exists()

    ctx.controller.handle(
        WorkspaceMemoryWrite(scope="specific", workspace_id=workspace_id,
                             mode="replace", text="Only the replacement remains.")
    )
    saved = target.read_text(encoding="utf-8")
    assert saved.strip() == "Only the replacement remains."
    assert target.with_suffix(".md.bak").read_text(encoding="utf-8").find("Use the project conventions.") >= 0


def test_workspace_memory_cascade_reaches_model_in_general_to_specific_order(tmp_path, monkeypatch):
    gateway = MockGateway()
    ctx, events = _boot(tmp_path, monkeypatch, gateway)
    external = tmp_path / "project-memory"
    external.mkdir()
    try:
        ctx.controller.handle(WorkspaceMemoryWrite(scope="default", text="DEFAULT-RULE"))
        ctx.controller.handle(WorkspaceCreate(name="Project", root_kind="external", root=str(external)))
        workspace_id = _last(events, "workspace.list").current
        ctx.controller.handle(WorkspaceMemoryWrite(scope="current", text="ACTIVE-RULE"))
        ctx.controller.handle(NewSession(title="scoped", workspace_id=workspace_id))
        ctx.controller.handle(SendMessage(text="test memory order"))

        system = gateway.calls[-1]["messages"][0]["content"]
        assert "DEFAULT-RULE" in system and "ACTIVE-RULE" in system
        assert system.index("DEFAULT-RULE") < system.index("ACTIVE-RULE")
    finally:
        ctx.worker.stop()


def test_workspace_build_runs_in_workspace_and_writes_artifacts(tmp_path, monkeypatch):
    """切片 4：构建命令在 A 的 root 执行、日志落 artifacts/<ts>/、审计不记命令原文。"""
    import sys

    from core.shell import manager as shell_module
    from core.shell.process import Interpreter

    fake = Path(__file__).resolve().parents[1] / "mocks" / "fake_shell.py"
    interpreter = Interpreter(kind="bash", path=sys.executable, argv=(sys.executable, "-u", str(fake)))
    monkeypatch.setattr(shell_module, "detect", lambda _kind="auto": interpreter)
    ctx, events = _boot(tmp_path, monkeypatch)
    external = tmp_path / "build-project"
    external.mkdir()
    try:
        ctx.controller.handle(
            WorkspaceCreate(name="构建项目", root_kind="external", root=str(external),
                            build_cmd="echo building-artifact")
        )
        workspace_id = _last(events, "workspace.list").current
        ctx.controller.handle(WorkspaceBuild(id=workspace_id))

        result = _last(events, "workspace.build.result")
        assert result is not None and result.ok is True
        assert result.exit_code == 0
        assert "building-artifact" in result.output
        log_path = Path(result.log_path)
        assert log_path.exists() and "building-artifact" in log_path.read_text(encoding="utf-8")
        metadata = json.loads((log_path.parent / "build.json").read_text(encoding="utf-8"))
        assert metadata["command"] == "echo building-artifact"
        assert metadata["exit_code"] == 0
        # 审计不记命令原文（沿 shell 先例）
        audit_text = (Path(ctx.root) / "logs" / "audit.jsonl").read_text(encoding="utf-8")
        assert "building-artifact" not in audit_text
        assert "workspace.build" in audit_text
    finally:
        ctx.worker.stop()


def test_workspace_build_without_command_reports_readable_error(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    try:
        ctx.controller.handle(WorkspaceCreate(name="无命令"))
        workspace_id = _last(events, "workspace.list").current
        count = len([e for e in events if e.type == "error"])
        ctx.controller.handle(WorkspaceBuild(id=workspace_id))
        errors = [e for e in events if e.type == "error"]
        assert len(errors) > count
        assert errors[-1].code == "invalid_request"
        assert not [e for e in events if e.type == "workspace.build.result"]
    finally:
        ctx.worker.stop()


def test_workspace_attachments_are_resolved_and_injected_as_bounded_text(tmp_path, monkeypatch):
    gateway = MockGateway()
    ctx, events = _boot(tmp_path, monkeypatch, gateway)
    external = tmp_path / "attachment-project"
    external.mkdir()
    (external / "notes.txt").write_text("ATTACHMENT-CONTENT api_key=sk-abc123456789", encoding="utf-8")
    try:
        ctx.controller.handle(WorkspaceCreate(name="Files", root_kind="external", root=str(external)))
        workspace_id = _last(events, "workspace.list").current
        ctx.controller.handle(NewSession(title="attachment", workspace_id=workspace_id))
        session_id = ctx.controller.current_session_id
        ctx.controller.handle(SendMessage(text="read this", attachments=["notes.txt"]))

        system = gateway.calls[-1]["messages"][0]["content"]
        assert "ATTACHMENT-CONTENT" in system
        assert "notes.txt" in system
        assert "sk-abc123456789" not in system
        usage = [
            event for event in ctx.session_store.sink.read_events(session_id)
            if event.get("type") == "ctx.usage"
        ][-1]
        assert usage["payload"]["segments"]["files"] > 0

        calls_before = len(gateway.calls)
        ctx.controller.handle(SendMessage(text="escape", attachments=["../outside.txt"]))
        error = _last(events, "error")
        assert error is not None and error.code == "invalid_request"
        assert len(gateway.calls) == calls_before
    finally:
        ctx.worker.stop()


def test_shell_exec_and_manual_spawn_start_in_session_workspace(tmp_path, monkeypatch):
    """slice 2：模型 shell.exec 与终端页手动 spawn 都以所属 workspace root 起壳。"""
    import sys

    from core.shell import manager as shell_module
    from core.shell.process import Interpreter

    fake = Path(__file__).resolve().parents[1] / "mocks" / "fake_shell.py"
    interpreter = Interpreter(
        kind="bash", path=sys.executable, argv=(sys.executable, "-u", str(fake))
    )
    monkeypatch.setattr(shell_module, "detect", lambda _kind="auto": interpreter)
    gateway = MockGateway(
        tool_call_rounds=[
            [{"id": "call_workspace_shell", "name": "shell.exec",
              "arguments": json.dumps({"command": "pwd"})}]
        ]
    )
    ConfigStore(tmp_path / "ymtdata").save(
        "modules", ModulesConfig(shell=ShellConfig(tool_permissions={"shell.exec": "safe"}))
    )
    ctx, events = _boot(tmp_path, monkeypatch, gateway)
    root_a = tmp_path / "project-a"
    root_a.mkdir()
    root_b = tmp_path / "project-b"
    root_b.mkdir()
    try:
        ctx.controller.handle(WorkspaceCreate(name="A", root_kind="external", root=str(root_a)))
        workspace_id = _last(events, "workspace.list").current
        ctx.controller.handle(NewSession(title="A session", workspace_id=workspace_id))
        session_id = ctx.controller.current_session_id
        ctx.controller.handle(SendMessage(text="where am I?"))

        shell = ctx.features.host("shell")
        assert shell is not None
        status = shell.list_status()[0]
        assert Path(status["cwd"]) == root_a
        assert status["workspace_id"] == workspace_id
        tool_result = next(
            event for event in reversed(ctx.session_store.sink.read_events(session_id))
            if event.get("type") == "tool.result"
        )
        assert str(root_a) in tool_result["payload"]["output"]

        ctx.controller.handle(ShellSpawn())
        assert len(shell.list_status()) == 2
        assert {row["workspace_id"] for row in shell.list_status()} == {workspace_id}

        # 更换 root 后关闭旧 cwd 的持久 shell；下一次调用再按新 root 惰性重建。
        ctx.controller.handle(
            WorkspaceUpdate(id=workspace_id, name="A", root=str(root_b))
        )
        assert shell.list_status() == []
    finally:
        ctx.worker.stop()

    ctx.controller.handle(MoveSession(session_id=session_id, workspace_id=workspace_id))
    assert _meta_of(ctx, session_id)["workspace_id"] == workspace_id
    # 再挪回默认工作区（归属为 None）
    ctx.controller.handle(MoveSession(session_id=session_id, workspace_id=None))
    assert _meta_of(ctx, session_id)["workspace_id"] is None


def test_move_session_to_unknown_workspace_reports_error(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(NewSession(title="会话"))
    session_id = ctx.controller.current_session_id
    ctx.controller.handle(MoveSession(session_id=session_id, workspace_id="ws_nope"))
    err = _last(events, "error")
    assert err is not None
    assert err.code == "invalid_request"
    assert _meta_of(ctx, session_id)["workspace_id"] is None  # 失败不得改归属


# -- 更新 / 移除 ---------------------------------------------------------------
def test_update_workspace_persists_meta(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(WorkspaceCreate(name="旧名"))
    workspace_id = _last(events, "workspace.list").current

    ctx.controller.handle(
        WorkspaceUpdate(id=workspace_id, name="新名", note="备注", build_cmd="make all")
    )

    item = next(w for w in _last(events, "workspace.list").workspaces if w.id == workspace_id)
    assert (item.name, item.note, item.build_cmd) == ("新名", "备注", "make all")


def test_remove_workspace_keeps_files_and_resets_session_owner(tmp_path, monkeypatch):
    """R2 的端到端断言：移除工作区后磁盘文件仍在，且其会话**回落到默认工作区**。

    「回落」而不是「留悬空 id」：与 `docs/03` §7「引用即警告」同族口径 ——
    删掉被引用的对象时，受影响方回退到默认，否则侧栏会长期挂着一个不存在的工作区分组。
    """
    ctx, events = _boot(tmp_path, monkeypatch)
    ext = tmp_path / "precious"
    ext.mkdir()
    (ext / "keep.txt").write_text("别删我", encoding="utf-8")
    ctx.controller.handle(WorkspaceCreate(name="珍贵", root_kind="external", root=str(ext)))
    workspace_id = _last(events, "workspace.list").current
    ctx.controller.handle(NewSession(title="会话"))
    session_id = ctx.controller.current_session_id
    assert _meta_of(ctx, session_id)["workspace_id"] == workspace_id

    ctx.controller.handle(WorkspaceDelete(id=workspace_id))

    assert (ext / "keep.txt").read_text(encoding="utf-8") == "别删我"
    assert (ext / ".ymtdata").is_dir()
    listing = _last(events, "workspace.list")
    assert all(w.id != workspace_id for w in listing.workspaces)
    assert listing.current == WS_DEFAULT
    assert _meta_of(ctx, session_id)["workspace_id"] is None  # 归属已回落并落盘
    index = _last(events, "session.index")
    assert next(s for s in index.sessions if s.id == session_id).workspace_id is None


def test_remove_default_workspace_is_denied(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(WorkspaceDelete(id=WS_DEFAULT))
    err = _last(events, "error")
    assert err is not None
    assert err.code == "workspace_denied"
    assert any(w.id == WS_DEFAULT for w in _last(events, "workspace.list").workspaces)


# -- 详情与文件列表 -----------------------------------------------------------
def test_detail_returns_bounded_file_list(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ext = tmp_path / "proj"
    (ext / "src").mkdir(parents=True)
    (ext / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    ctx.controller.handle(WorkspaceCreate(name="项目", root_kind="external", root=str(ext)))
    workspace_id = _last(events, "workspace.list").current

    ctx.controller.handle(WorkspaceDetail(id=workspace_id))

    result = _last(events, "workspace.detail.result")
    assert result.error is None
    assert "src/main.py" in {entry["path"] for entry in result.entries}
    assert result.root == str(ext)


def test_detail_of_unknown_workspace_reports_error(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(WorkspaceDetail(id="ws_nope"))
    assert _last(events, "error") is not None
    assert _last(events, "workspace.detail.result") is None


# -- 折叠态（走既有 settings.update，不新增请求类型）--------------------------
def test_collapsed_state_round_trips_through_settings(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ext = tmp_path / "proj"
    ext.mkdir()
    ctx.controller.handle(WorkspaceCreate(name="项目", root_kind="external", root=str(ext)))
    workspace_id = _last(events, "workspace.list").current

    ctx.controller.handle(SettingsUpdate(section="ui", data={"collapsed_workspaces": [workspace_id]}))

    assert _last(events, "workspace.list").collapsed == [workspace_id]
    saved = json.loads((Path(ctx.root) / "config" / "settings.json").read_text(encoding="utf-8"))
    assert saved["ui"]["collapsed_workspaces"] == [workspace_id]
    # 主题/字号不受影响（同分区扩字段不该打翻既有取值）
    assert saved["ui"]["theme"] == "light"


# -- 首屏与既有行为 -----------------------------------------------------------
def test_initial_push_includes_default_workspace(tmp_path, monkeypatch):
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.push_initial_state()
    listing = _last(events, "workspace.list")
    assert listing is not None
    assert [w.id for w in listing.workspaces] == [WS_DEFAULT]
    assert listing.current == WS_DEFAULT


def test_resume_and_replay_still_work_with_workspace_field(tmp_path, monkeypatch):
    """加字段不许打翻既有会话链路：恢复/回放照常。"""
    ctx, events = _boot(tmp_path, monkeypatch)
    ctx.controller.handle(NewSession(title="会话"))
    session_id = ctx.controller.current_session_id
    ctx.controller.handle(ResumeSession(session_id=session_id))
    assert _last(events, "session.events") is not None
