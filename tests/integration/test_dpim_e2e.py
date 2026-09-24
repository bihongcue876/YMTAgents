"""DPIM 端到端：从默认真卸载，经信封启用/建库/索引/只读检索到真卸载。"""

from __future__ import annotations

import sys
from pathlib import Path

from app import bootstrap as bootstrap_mod
from app import paths
from shared.envelope import FeatureToggle, LibraryCreate, LibraryIngest, LibraryQuery
from tests.mocks.dpim_gateway import DpimGateway


def _boot(tmp_path, monkeypatch, gateway):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    return bootstrap_mod.bootstrap(gateway_factory=lambda _store: gateway)


def test_dpim_lifecycle_ingest_query_redaction_and_unload(tmp_path, monkeypatch, qapp):
    gateway = DpimGateway()
    ctx = _boot(tmp_path, monkeypatch, gateway)
    events = []
    ctx.bridge.event_received.connect(events.append)
    try:
        # 初始关闭只登记惰性 factory：不 import DPIM、不读库数据、不注册工具。
        assert ctx.features.enabled("dpim") is False
        assert ctx.features.host("dpim") is None
        assert "core.modules.dpim.manager" not in sys.modules
        assert not any(spec.name.startswith("dpim.") for spec in ctx.registry.snapshot())

        ctx.controller.handle(FeatureToggle(name="dpim", enabled=True))
        host = ctx.features.host("dpim")
        assert host is not None and host.host_state() == "ready"
        assert {spec.name for spec in ctx.registry.snapshot()} >= {"dpim.query", "dpim.joint_query"}

        ctx.controller.handle(
            LibraryCreate(name="forbidden", root_kind="external", root=str(Path.home()))
        )
        denied = [event for event in events if event.type == "error" and event.scope == "library"][-1]
        assert denied.code == "library_denied"

        ctx.controller.handle(LibraryCreate(name="Architecture", model_ref="main", group="YMT"))
        created = [event for event in events if event.type == "library.list"][-1]
        library_id = created.libraries[0]["id"]
        library_root = Path(created.libraries[0]["resolved_root"])
        assert created.current == library_id

        ctx.controller.handle(
            LibraryIngest(
                id=library_id,
                text="YMT memory stores local knowledge. api_key=sk-abc123456789 is not retained.",
                event_type="data",
            )
        )
        ingested = [event for event in events if event.type == "library.ingest.result"][-1]
        assert ingested.status == "linked"
        event_id = ingested.event_id
        assert len(gateway.agent_calls) == 4
        assert all("sk-abc123456789" not in json_text for _, payload in gateway.agent_calls
                   for json_text in [str(payload)])

        ctx.controller.handle(LibraryQuery(query="YMT", lib_ids=[library_id]))
        result = [event for event in events if event.type == "library.query.result"][-1]
        assert result.results
        assert result.results[0]["source_refs"]

        ctx.controller.handle(LibraryIngest(id=library_id, text="^help"))
        help_result = [event for event in events if event.type == "library.ingest.result"][-1]
        assert "^node add" in help_result.message
        ctx.controller.handle(
            LibraryIngest(id=library_id, text="^node add Manual note | User-authored graph note")
        )
        manual_node = [event for event in events if event.type == "library.ingest.result"][-1]
        assert "图节点修改" in manual_node.message
        ctx.controller.handle(LibraryIngest(id=library_id, text=f"^delete {event_id}"))
        deleted = [event for event in events if event.type == "library.ingest.result"][-1]
        assert deleted.status == "skipped"
        ctx.controller.handle(LibraryQuery(query="YMT", lib_ids=[library_id]))
        after_delete = [event for event in events if event.type == "library.query.result"][-1]
        assert not after_delete.results

        index_path = ctx.root / "libraries" / "index.json"
        assert index_path.exists()
        assert any(library_root.rglob("memory.db"))

        ctx.controller.handle(FeatureToggle(name="dpim", enabled=False))
        assert ctx.features.host("dpim") is None
        assert not any(spec.name.startswith("dpim.") for spec in ctx.registry.snapshot())
        assert ctx.config_store.load("modules").features.dpim is False
        assert index_path.exists()
        assert any(library_root.rglob("memory.db"))
    finally:
        ctx.worker.stop()
