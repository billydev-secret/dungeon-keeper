"""Tests for services/intake_reference_service — bot-synced procedure docs.

The tested unit is the pure pipeline: block parsing/validation, message
rendering (one message per question), the position-wise sync differ, the
mapping bookkeeping, and import drafting. The Discord side (sync_channel /
import_channel) is glue over these.
"""

from __future__ import annotations

import json

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import intake_reference_service as ref
from migrations import apply_migrations_sync

GUILD = 42

BLOCKS = [
    {"kind": "text", "title": "How intake works", "body": "Greet them.\nBe kind."},
    {"kind": "questions", "title": "SFW questions", "body": "Q one?\n\nQ two?\n"},
]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ref.db"
    apply_migrations_sync(path)
    return path


# ── parse / validate ──────────────────────────────────────────────────


def test_parse_blocks_tolerant():
    assert ref.parse_blocks("") == []
    assert ref.parse_blocks("not json") == []
    assert ref.parse_blocks('{"kind": "text"}') == []  # not a list
    raw = json.dumps(
        BLOCKS
        + [
            "not a dict",
            {"kind": "telepathy", "title": "x", "body": "y"},
            {"kind": "text", "title": "", "body": "   "},  # empty
        ]
    )
    blocks = ref.parse_blocks(raw)
    assert [b.kind for b in blocks] == ["text", "questions"]
    assert blocks[0].title == "How intake works"


def test_validate_blocks_strict_and_canonical():
    stored = ref.validate_blocks(BLOCKS)
    assert ref.parse_blocks(stored) == ref.parse_blocks(json.dumps(BLOCKS))
    with pytest.raises(ValueError, match="unknown kind"):
        ref.validate_blocks([{"kind": "x", "title": "t", "body": "b"}])
    with pytest.raises(ValueError, match="title or some content"):
        ref.validate_blocks([{"kind": "text", "title": "", "body": " "}])
    with pytest.raises(ValueError, match="at least one question"):
        ref.validate_blocks([{"kind": "questions", "title": "t", "body": "\n \n"}])


# ── render ────────────────────────────────────────────────────────────


def test_render_questions_one_message_per_line():
    messages = ref.render_blocks(ref.parse_blocks(json.dumps(BLOCKS)))
    assert messages == [
        "**How intake works**\nGreet them.\nBe kind.",
        "**SFW questions**",
        "Q one?",
        "Q two?",
    ]


def test_render_questions_without_title_has_no_header():
    blocks = ref.parse_blocks(
        json.dumps([{"kind": "questions", "title": "", "body": "Only q?"}])
    )
    assert ref.render_blocks(blocks) == ["Only q?"]


def test_render_chunks_long_text_on_paragraphs():
    para = "x" * 1000
    blocks = [ref.Block("text", "", f"{para}\n\n{para}\n\n{para}")]
    messages = ref.render_blocks(blocks)
    # 1000+2+1000 > 1900, so each paragraph lands in its own message.
    assert len(messages) == 3
    assert all(len(m) <= 1900 for m in messages)
    # Nothing lost across the split.
    assert "".join(messages).count("x") == 3000
    # Two short paragraphs DO share one message.
    short = ref.render_blocks([ref.Block("text", "", "aaa\n\nbbb\n\n" + "z" * 1900)])
    assert short[0] == "aaa\n\nbbb"
    assert len(short) == 2


def test_render_hard_splits_pathological_line():
    blocks = [ref.Block("text", "", "y" * 4000)]
    messages = ref.render_blocks(blocks)
    assert all(len(m) <= 1900 for m in messages)
    assert sum(m.count("y") for m in messages) == 4000


# ── diff ──────────────────────────────────────────────────────────────


def _stored(contents):
    return [(100 + i, ref.content_hash(c)) for i, c in enumerate(contents)]


def test_diff_noop_when_unchanged():
    ops, deletes = ref.diff_messages(["a", "b"], _stored(["a", "b"]))
    assert ops == [("keep", 100, "a"), ("keep", 101, "b")]
    assert deletes == []


def test_diff_edits_in_place_and_posts_tail():
    ops, deletes = ref.diff_messages(["a", "B", "c"], _stored(["a", "b"]))
    assert ops == [("keep", 100, "a"), ("edit", 101, "B"), ("post", 0, "c")]
    assert deletes == []


def test_diff_deletes_surplus():
    ops, deletes = ref.diff_messages(["a"], _stored(["a", "b", "c"]))
    assert ops == [("keep", 100, "a")]
    assert deletes == [101, 102]


def test_diff_middle_insert_shifts_content_not_ids():
    # Inserting "x" after "a" edits the existing tail messages in place and
    # posts one new message at the end — ids never churn.
    ops, deletes = ref.diff_messages(["a", "x", "b"], _stored(["a", "b"]))
    assert ops == [("keep", 100, "a"), ("edit", 101, "x"), ("post", 0, "b")]
    assert deletes == []


# ── mapping bookkeeping ───────────────────────────────────────────────


def test_mapping_roundtrip_and_replace(db_path):
    with open_db(db_path) as conn:
        assert ref.stored_messages(conn, GUILD) == []
        ref.replace_mapping(conn, GUILD, [(11, "h1"), (22, "h2")])
        assert ref.stored_messages(conn, GUILD) == [(11, "h1"), (22, "h2")]
        ref.replace_mapping(conn, GUILD, [(33, "h3")])
        assert ref.stored_messages(conn, GUILD) == [(33, "h3")]
        # Other guilds are untouched.
        assert ref.stored_messages(conn, 99) == []


# ── import drafting ───────────────────────────────────────────────────


def test_blocks_from_messages_drafts_text_blocks():
    drafts = ref.blocks_from_messages(["First rule post", "", "  ", "Questions wall"])
    assert drafts == [
        {"kind": "text", "title": "", "body": "First rule post"},
        {"kind": "text", "title": "", "body": "Questions wall"},
    ]
