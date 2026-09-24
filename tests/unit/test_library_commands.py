from __future__ import annotations

import pytest

from core.modules.dpim.commands import parse_command, parse_node_add, split_words


def test_command_parser_preserves_free_text_payload():
    command = parse_command("  ^data  Words with 'quotes' and spaces  ")
    assert command is not None
    assert command.name == "data"
    assert command.arguments == "Words with 'quotes' and spaces"


def test_unknown_command_is_parsed_for_visible_rejection():
    command = parse_command("^unknown payload")
    assert command is not None and command.name == "unknown"


def test_node_add_parser_accepts_quoted_title_and_content():
    assert parse_node_add('"concept title" | "multi word content"') == (
        "concept title",
        "multi word content",
    )


def test_node_add_parser_requires_separator_and_content():
    with pytest.raises(ValueError):
        parse_node_add("only a title")
    with pytest.raises(ValueError):
        parse_node_add("title | ")


def test_structural_args_reject_unbalanced_quotes():
    with pytest.raises(ValueError):
        split_words('"unterminated')
