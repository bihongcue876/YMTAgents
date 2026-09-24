from __future__ import annotations

import json

import pytest

from core.library.graph import GraphStore, GraphStoreError
from core.library.line import LineStore
from core.library.manager import LibraryManager
from core.library.models import GraphNode
from core.library.paths import LibraryDenied, library_root, validate_external_root
from shared.ids import new_id


def test_managed_root_uses_generated_keys_not_display_names(tmp_path):
    manager = LibraryManager(tmp_path / "ymtdata")
    manager.load()
    library_id = manager.create_library("../name/does-not-become-path", group="x/../../group")
    record = manager._record(library_id)
    root = library_root(record, manager.data_root)

    assert root.name == library_id
    assert root.parent.name.startswith("grp_")
    assert "does-not-become-path" not in str(root)
    assert "x/../../group" not in str(root)


def test_external_root_denies_roots_home_and_data_root(tmp_path, monkeypatch):
    data_root = tmp_path / "ymtdata"
    safe = tmp_path / "book"
    safe.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    data_root.mkdir()
    monkeypatch.setattr("core.library.paths.Path.home", lambda: home)

    assert validate_external_root(str(safe), data_root) == safe
    with pytest.raises(LibraryDenied):
        validate_external_root(str(home), data_root)
    with pytest.raises(LibraryDenied):
        validate_external_root(str(data_root), data_root)


def test_index_corruption_is_preserved_and_fails_closed(tmp_path):
    root = tmp_path / "ymtdata"
    base = root / "libraries"
    base.mkdir(parents=True)
    index = base / "index.json"
    original = b"{ this is not valid json\n"
    index.write_bytes(original)

    manager = LibraryManager(root)
    manager.load()
    assert manager.error
    assert index.read_bytes() == original
    with pytest.raises(Exception):
        manager.create_library("must not overwrite")
    assert index.read_bytes() == original


def test_line_store_append_transition_and_chinese_like_fallback(tmp_path):
    line = LineStore(tmp_path / "lib")
    line._fts_available = False
    event = line.append(new_id("evt"), "中文检索资料", "data")
    assert [item.event_id for item in line.search("中文检索")] == [event.event_id]
    line.transition(event.event_id, "linked", graph_refs=["node_123"])
    assert line.get(event.event_id).status == "linked"
    with pytest.raises(ValueError):
        line.transition(event.event_id, "raw")


def test_graph_atomic_backup_recovery_preserves_good_backup(tmp_path):
    graph = GraphStore(tmp_path / "lib")
    first = GraphNode(node_id=new_id("node"), title="first", content="one")
    graph.upsert([first], [])
    second = GraphNode(node_id=new_id("node"), title="second", content="two")
    graph.upsert([second], [])
    good_backup = graph.backup_path.read_bytes()
    graph.path.write_text("not json", encoding="utf-8")

    restored = graph.load()
    assert graph.recovered_from_backup is True
    assert [node.title for node in restored.nodes] == ["first"]
    graph.save(restored)
    assert graph.path.exists()
    assert graph.backup_path.read_bytes() == good_backup


def test_graph_corruption_without_backup_does_not_create_empty_graph(tmp_path):
    graph = GraphStore(tmp_path / "lib")
    graph.root.mkdir(parents=True)
    graph.path.write_text("broken", encoding="utf-8")
    original = graph.path.read_bytes()
    with pytest.raises(GraphStoreError):
        graph.load()
    assert graph.path.read_bytes() == original


def test_reconcile_repairs_bidirectional_source_references(tmp_path):
    line = LineStore(tmp_path / "lib")
    graph = GraphStore(tmp_path / "lib")
    event = line.append(new_id("evt"), "source text", "source")
    node = GraphNode(node_id=new_id("node"), title="source node", source_refs=[event.event_id])
    graph.upsert([node], [])

    result = graph.reconcile(line)
    assert result["events"] == 1
    assert line.get(event.event_id).graph_refs == [node.node_id]
    assert line.get(event.event_id).status == "linked"


def test_joint_query_deduplicates_but_keeps_each_library_anchor(tmp_path):
    manager = LibraryManager(tmp_path / "ymtdata")
    manager.load()
    first = manager.create_library("one")
    second = manager.create_library("two")
    manager.append_event(first, "shared source phrase", "data")
    manager.append_event(second, "shared source phrase", "data")

    result = manager.query("shared source phrase", top_k=8)
    assert len(result["results"]) == 1
    assert {row["library_id"] for row in result["results"][0]["source_refs"]} == {first, second}
    assert len(result["debug"]) == 2


def test_read_only_query_does_not_modify_store_bytes(tmp_path):
    manager = LibraryManager(tmp_path / "ymtdata")
    manager.load()
    library_id = manager.create_library("readonly")
    manager.append_event(library_id, "immutable query source", "data")
    manager.switch_library(library_id)
    record = manager._record(library_id)
    line, graph = manager._stores(record)
    graph.reconcile(line)
    db_path = line.path
    graph_path = graph.path
    if not graph_path.exists():
        graph.save(graph.load())
    before = (db_path.read_bytes(), graph_path.read_bytes())

    manager.query("immutable query", [library_id])

    assert (db_path.read_bytes(), graph_path.read_bytes()) == before


def test_remove_registration_keeps_external_files(tmp_path):
    root = tmp_path / "ymtdata"
    external = tmp_path / "external"
    external.mkdir()
    (external / "memo.txt").write_text("keep", encoding="utf-8")
    manager = LibraryManager(root)
    manager.load()
    library_id = manager.create_library("external", root_kind="external", root=str(external))
    manager.delete_library(library_id)
    assert (external / "memo.txt").read_text(encoding="utf-8") == "keep"
