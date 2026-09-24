from __future__ import annotations

from core.memory.cascade import cascade_paths, read_cascade


def test_four_layer_cascade_is_general_to_specific_and_deduplicated(tmp_path):
    root = tmp_path / "ymtdata"
    default = root / "workspace"
    active = tmp_path / "project" / ".ymtdata"
    session = root / "sessions" / "sess_1"
    paths = [root / "config" / "AGENTS.md", default / "AGENTS.md", active / "AGENTS.md",
             session / "AGENTS.md"]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"layer-{index}", encoding="utf-8")

    dirs = [default, default, active]
    assert cascade_paths(root, "sess_1", dirs) == [paths[0], paths[1], paths[2], paths[3]]
    assert read_cascade(root, "sess_1", dirs) == "\n\n".join(f"layer-{i}" for i in range(4))


def test_legacy_cascade_without_injected_workspace_paths_is_unchanged(tmp_path):
    root = tmp_path / "ymtdata"
    files = [root / "config" / "AGENTS.md", root / "workspace" / "AGENTS.md",
             root / "sessions" / "sess_1" / "AGENTS.md"]
    for index, path in enumerate(files):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"legacy-{index}", encoding="utf-8")
    assert read_cascade(root, "sess_1") == "\n\n".join(f"legacy-{i}" for i in range(3))
