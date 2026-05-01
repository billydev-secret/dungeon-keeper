"""Tests for cogs.todo_cog helpers and the context-menu modal."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cogs.todo_cog import _format_description, _format_task_label, _MAX_CONTENT_LEN  # noqa: E402


# ── _format_task_label ────────────────────────────────────────────────


def test_label_uses_display_name_and_channel_name():
    assert (
        _format_task_label(author_display="Alice", channel_name="general")
        == "Message from @Alice in #general"
    )


# ── _format_description ───────────────────────────────────────────────


def test_description_message_only():
    assert _format_description(message_content="hello world", notes="") == "hello world"


def test_description_notes_only_uses_no_text_marker():
    assert (
        _format_description(message_content="", notes="follow up next week")
        == "[no text content]\n\nfollow up next week"
    )


def test_description_both_joined_with_blank_line():
    assert (
        _format_description(message_content="hello", notes="follow up")
        == "hello\n\nfollow up"
    )


def test_description_neither_uses_no_text_marker():
    assert _format_description(message_content="", notes="") == "[no text content]"


def test_description_truncates_long_content():
    long = "x" * (_MAX_CONTENT_LEN + 50)
    out = _format_description(message_content=long, notes="")
    assert out.endswith("…")
    assert len(out) == _MAX_CONTENT_LEN + 1  # MAX chars + the ellipsis


def test_description_truncates_long_content_with_notes():
    long = "x" * (_MAX_CONTENT_LEN + 50)
    out = _format_description(message_content=long, notes="note")
    head, _, tail = out.partition("\n\n")
    assert head.endswith("…")
    assert len(head) == _MAX_CONTENT_LEN + 1
    assert tail == "note"
