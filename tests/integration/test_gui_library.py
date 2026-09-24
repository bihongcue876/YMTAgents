from __future__ import annotations

from PySide6.QtCore import Qt

from gui.pages.library import LibraryPage
from shared.envelope import LibraryGraphResult, LibraryList


def test_library_page_uses_plain_native_graph_and_routes_source_anchor(qapp):
    page = LibraryPage()
    page.set_available(True, "ready")
    page.on_list(
        LibraryList(
            libraries=[{"id": "lib_1", "name": "Knowledge", "group": None}],
            current="lib_1",
        )
    )
    page.on_graph(
        LibraryGraphResult(
            library_ids=["lib_1"],
            nodes=[
                {
                    "node_id": "node_1",
                    "title": "<script>plain text</script>",
                    "content": "untrusted <img src=x>",
                    "node_type": "data",
                    "source_refs": ["evt_1"],
                    "library_id": "lib_1",
                    "library_name": "Knowledge",
                }
            ],
            edges=[],
        )
    )
    node_items = [item for item in page._graph._scene.items() if item.data(Qt.UserRole)]
    assert len(node_items) == 1
    node_items[0].setSelected(True)

    requests = []
    page.detail_requested.connect(lambda *args: requests.append(args))
    page._graph_source.click()
    assert requests
    assert requests[-1][0] == "lib_1"
    assert requests[-1][-1] == "evt_1"
    assert page._graph_info.toPlainText().find("<script>") >= 0

    page.on_graph(
        LibraryGraphResult(
            library_ids=["lib_1", "lib_2"],
            nodes=[
                {"node_id": "a", "title": "A", "library_id": "lib_1"},
                {"node_id": "b", "title": "B", "library_id": "lib_2"},
            ],
            edges=[],
        )
    )
    assert not page._graph_compare.isHidden()
    assert page._graph._scene.items()
    assert page._graph_compare._scene.items()
