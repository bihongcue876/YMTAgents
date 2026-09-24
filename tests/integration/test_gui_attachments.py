from __future__ import annotations

from gui.chat import input_bar as input_bar_module
from gui.chat.input_bar import InputBar


def test_input_bar_emits_workspace_relative_attachment_paths(tmp_path, monkeypatch, qapp):
    root = tmp_path / "workspace"
    root.mkdir()
    file_path = root / "notes.txt"
    file_path.write_text("text", encoding="utf-8")
    monkeypatch.setattr(
        input_bar_module.QFileDialog,
        "getOpenFileNames",
        lambda *_args, **_kwargs: ([str(file_path)], ""),
    )
    bar = InputBar()
    bar.set_attachment_root(root)
    sent = []
    bar.send_message.connect(lambda text, attachments: sent.append((text, attachments)))

    bar._pick_attachments()
    bar._on_send()  # 附件可以单独发送，不要求重复填写提示文字

    assert sent == [("", ["notes.txt"])]
    assert bar._attachments == []
