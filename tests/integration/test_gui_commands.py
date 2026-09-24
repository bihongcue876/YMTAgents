from __future__ import annotations

from gui.chat.input_bar import InputBar


def test_command_palette_filters_and_runs_selected_command(qapp):
    bar = InputBar()
    runs = []
    errors = []
    bar.command_run.connect(lambda name, action, argument: runs.append((name, action, argument)))
    bar.command_error.connect(errors.append)

    bar._edit.setPlainText("/th")
    qapp.processEvents()
    assert not bar._palette.isHidden()  # 顶层未 show，用 isHidden 而非 isVisible
    # 首个匹配是 theme（按命令表顺序）
    assert bar._palette.current_command().name == "theme"

    bar._edit.setPlainText("/theme dark")
    bar._run_command()
    assert runs == [("theme", "theme", "dark")]
    assert bar._edit.toPlainText() == ""
    assert bar._palette.isHidden()


def test_unknown_command_is_reported_and_not_sent_as_message(qapp):
    bar = InputBar()
    runs = []
    errors = []
    sent = []
    bar.command_run.connect(lambda *args: runs.append(args))
    bar.command_error.connect(errors.append)
    bar.send_message.connect(lambda text, attachments: sent.append((text, attachments)))

    bar._edit.setPlainText("/bogus")
    bar._on_send()

    assert runs == []
    assert sent == []  # 命令态文本绝不作为普通消息发出
    assert errors and "未知命令" in errors[-1]


def test_plain_message_still_sends(qapp):
    bar = InputBar()
    sent = []
    bar.send_message.connect(lambda text, attachments: sent.append((text, attachments)))
    bar._edit.setPlainText("普通消息")
    bar._on_send()
    assert sent == [("普通消息", [])]