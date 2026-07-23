"""Tests for services/intake_reference_service — bot-synced procedure docs.

The tested unit is the pure pipeline: block parsing/validation, message
rendering (one message per question), the position-wise sync differ, the
mapping bookkeeping, and import drafting. The Discord side (sync_channel /
import_channel) is glue over these.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
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
    # A question longer than one Discord message would 400 mid-sync and
    # wedge the reconcile — rejected on save instead.
    with pytest.raises(ValueError, match="the limit is"):
        ref.validate_blocks(
            [{"kind": "questions", "title": "t", "body": "q" * 2500}]
        )


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


def test_chunking_preserves_order_around_an_oversized_line():
    # Regression: hard-split pieces used to be emitted while earlier text
    # was still buffered, so the intro posted AFTER the middle of the line
    # it introduces.
    chunks = ref._chunk_text("Intro paragraph\n\n" + "X" * 4000)
    assert chunks[0] == "Intro paragraph"
    assert chunks[1] == "X" * 1900
    assert "".join(chunks).startswith("Intro paragraph")
    assert sum(c.count("X") for c in chunks) == 4000


def test_chunking_keeps_single_newlines_inside_a_paragraph():
    # Regression: line pieces of an oversized paragraph were rejoined with
    # "\n\n", turning single newlines into blank lines in the channel.
    body = "\n".join(["a" * 700, "b" * 700, "c" * 700])
    chunks = ref._chunk_text(body)
    assert "\n\n" not in "".join(chunks)
    assert chunks[0] == f"{'a' * 700}\n{'b' * 700}"


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


# ── sync failure paths ────────────────────────────────────────────────


class _FakeResponse:
    """Enough of an aiohttp response for discord.HTTPException's formatter."""

    def __init__(self, status):
        self.status = status
        self.reason = "Fake"


class _FakePartial:
    def __init__(self, channel, mid):
        self._channel, self._mid = channel, mid

    async def edit(self, content):
        if self._mid in self._channel.edit_fails:
            raise discord.HTTPException(_FakeResponse(500), "boom")
        self._channel.edits.append((self._mid, content))

    async def delete(self):
        self._channel.deletes.append(self._mid)


class _FakeChannel(discord.TextChannel):
    """Minimal stand-in: records sends/edits/deletes, can fail on demand."""

    def __init__(self, *, send_fails_at=None, edit_fails=()):
        self.id = 555
        self.sent, self.edits, self.deletes = [], [], []
        self.send_fails_at = send_fails_at  # nth send (0-based) raises
        self.edit_fails = set(edit_fails)
        self._next_id = 1000

    async def send(self, content, **kwargs):
        if self.send_fails_at is not None and len(self.sent) == self.send_fails_at:
            raise discord.HTTPException(_FakeResponse(400), "too long")
        self._next_id += 1
        self.sent.append(content)
        return SimpleNamespace(id=self._next_id)

    def get_partial_message(self, mid):
        return _FakePartial(self, mid)


class _Ctx:
    def __init__(self, db_path):
        self.db_path = db_path

    def open_db(self):
        return open_db(self.db_path)


def _guild(channel):
    return SimpleNamespace(id=GUILD, get_channel=lambda cid: channel)


def _setup(db_path, blocks, channel_id=555):
    with open_db(db_path) as conn:
        set_config_value(conn, ref.CHANNEL_KEY, str(channel_id), GUILD)
        set_config_value(conn, ref.BLOCKS_KEY, ref.validate_blocks(blocks), GUILD)


async def test_sync_posts_and_maps(db_path):
    _setup(db_path, [{"kind": "questions", "title": "SFW", "body": "Q1?\nQ2?"}])
    channel = _FakeChannel()
    result = await ref.sync_channel(_Ctx(db_path), _guild(channel))
    assert result["posted"] == 3 and result["incomplete"] is False
    assert channel.sent == ["**SFW**", "Q1?", "Q2?"]
    with open_db(db_path) as conn:
        assert len(ref.stored_messages(conn, GUILD)) == 3


async def test_failed_edit_keeps_old_hash_so_it_retries(db_path):
    # Regression: the failed-edit branch stored the INTENDED hash, so the
    # next diff said "keep" and the stale message was never fixed.
    _setup(db_path, [{"kind": "text", "title": "", "body": "original"}])
    ctx = _Ctx(db_path)
    channel = _FakeChannel()
    await ref.sync_channel(ctx, _guild(channel))
    with open_db(db_path) as conn:
        (mid, _), = ref.stored_messages(conn, GUILD)

    _setup(db_path, [{"kind": "text", "title": "", "body": "reworded"}])
    failing = _FakeChannel(edit_fails=[mid])
    result = await ref.sync_channel(ctx, _guild(failing))
    assert result["incomplete"] is True
    assert failing.edits == []  # the edit really failed

    # Next save (Discord healthy) must still see work to do.
    ok = _FakeChannel()
    result = await ref.sync_channel(ctx, _guild(ok))
    assert ok.edits == [(mid, "reworded")]
    assert result["edited"] == 1 and result["incomplete"] is False


async def test_failed_post_keeps_earlier_messages_tracked(db_path):
    # Regression: a send failure truncated the mapping, orphaning already-
    # tracked messages at later positions (the bot then refuses to touch
    # them, and reposts duplicates once the bad content is fixed).
    _setup(db_path, [{"kind": "questions", "title": "", "body": "Q1?\nQ2?\nQ3?"}])
    ctx = _Ctx(db_path)
    await ref.sync_channel(ctx, _guild(_FakeChannel()))
    with open_db(db_path) as conn:
        before = ref.stored_messages(conn, GUILD)
    assert len(before) == 3

    # Insert a question at the top: ops become edit,edit,edit,post — and
    # the trailing post (the 4th message) fails.
    _setup(db_path, [{"kind": "questions", "title": "", "body": "Q0?\nQ1?\nQ2?\nQ3?"}])
    failing = _FakeChannel(send_fails_at=0)
    result = await ref.sync_channel(ctx, _guild(failing))
    assert result["incomplete"] is True
    with open_db(db_path) as conn:
        after = ref.stored_messages(conn, GUILD)
    # All three original messages stay tracked (shifted content), nothing
    # orphaned, and the missing 4th position is retried on the next save.
    assert [m for m, _ in after] == [m for m, _ in before]

    ok = _FakeChannel()
    result = await ref.sync_channel(ctx, _guild(ok))
    assert ok.sent == ["Q3?"]  # only the missing tail is posted
    assert result["incomplete"] is False
    with open_db(db_path) as conn:
        assert len(ref.stored_messages(conn, GUILD)) == 4


def test_blocks_from_messages_drafts_text_blocks():
    drafts = ref.blocks_from_messages(["First rule post", "", "  ", "Questions wall"])
    assert drafts == [
        {"kind": "text", "title": "", "body": "First rule post"},
        {"kind": "text", "title": "", "body": "Questions wall"},
    ]
