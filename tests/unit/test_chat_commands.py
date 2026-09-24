from __future__ import annotations

from gui.chat import commands


def test_non_command_text_is_not_parsed_as_command():
    assert commands.parse("hello world") == (None, "", "")
    assert commands.is_command("hello") is False


def test_known_command_parses_with_argument():
    command, argument, error = commands.parse("/theme dark")
    assert error == ""
    assert command is not None and command.name == "theme" and command.action == "theme"
    assert argument == "dark"


def test_unknown_command_is_explicit_error_not_silent():
    command, argument, error = commands.parse("/nope")
    assert command is None
    assert argument == ""
    assert "未知命令" in error


def test_empty_command_asks_for_name():
    command, _argument, error = commands.parse("/")
    assert command is None
    assert error


def test_match_filters_by_prefix_case_insensitively():
    names = {command.name for command in commands.match("/TH")}
    assert names == {"theme", "thinking"}
    assert len(commands.match("/")) == len(commands.COMMANDS)


def test_help_text_lists_every_command():
    text = commands.help_text()
    for command in commands.COMMANDS:
        assert f"/{command.name}" in text