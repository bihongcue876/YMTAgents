from __future__ import annotations

import pytest

from core.agent.attachments import AttachmentError, load_attachments


def test_attachment_resolution_prefers_workspace_then_legacy_default_and_redacts(tmp_path):
    workspace = tmp_path / "workspace"
    legacy = tmp_path / "ymtdata" / "workspace" / "files"
    workspace.mkdir()
    legacy.mkdir(parents=True)
    (workspace / "current.txt").write_text("current api_key=sk-abcdef123456", encoding="utf-8")
    (legacy / "old.txt").write_text("legacy text", encoding="utf-8")

    files = load_attachments(workspace, ["current.txt", "old.txt", "current.txt"], legacy)
    assert [name for name, _ in files] == ["current.txt", "old.txt"]
    assert "sk-abcdef123456" not in files[0][1]
    assert "legacy text" in files[1][1]


def test_attachment_paths_reject_escape_absolute_and_binary(tmp_path):
    workspace = tmp_path / "workspace"
    legacy = tmp_path / "legacy"
    workspace.mkdir()
    legacy.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("outside", encoding="utf-8")
    (workspace / "binary.txt").write_bytes(b"\xff\xfe")

    with pytest.raises(AttachmentError):
        load_attachments(workspace, ["../outside.txt"], legacy)
    with pytest.raises(AttachmentError):
        load_attachments(workspace, [str(secret)], legacy)
    with pytest.raises(AttachmentError):
        load_attachments(workspace, ["binary.txt"], legacy)


def test_attachment_count_is_bounded(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(AttachmentError):
        load_attachments(workspace, [f"{index}.txt" for index in range(21)], workspace)
