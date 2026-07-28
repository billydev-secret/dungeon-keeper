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
from tests.db_template import migrated_db

GUILD = 42

BLOCKS = [
    {"kind": "text", "title": "How intake works", "body": "Greet them.\nBe kind."},
    {"kind": "questions", "title": "SFW questions", "body": "Q one?\n\nQ two?\n"},
]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ref.db"
    migrated_db(path)
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
        "**How intake works**",
        "Greet them.\nBe kind.",
        "**SFW questions**",
        "Q one?",
        "Q two?",
    ]


def test_render_title_is_its_own_message_so_the_body_copies_clean():
    # Most text blocks are canned messages a greeter copy-pastes, and
    # Discord's Copy Text takes the whole message — a heading sharing the
    # message means trimming it off every paste.
    blocks = [ref.Block("text", "Example Greeting", "Hi, welcome!")]
    assert ref.render_blocks(blocks) == ["**Example Greeting**", "Hi, welcome!"]


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        pytest.param(
            ref.Block("questions", "", "Only q?"), ["Only q?"], id="questions-no-title"
        ),
        pytest.param(
            ref.Block("text", "", "Just body"), ["Just body"], id="text-no-title"
        ),
        # validate_blocks allows a title with no body (a bare section heading).
        pytest.param(
            ref.Block("text", "Heading only", "  "),
            ["**Heading only**"],
            id="text-no-body",
        ),
    ],
)
def test_render_omits_the_half_that_is_empty(block, expected):
    assert ref.render_blocks([block]) == expected


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


# ── diff: hand-deleted messages ───────────────────────────────────────


def test_diff_rebuilds_from_a_hand_deleted_message():
    # Regression: an unchanged block hashed "keep" at every position, so a
    # message someone deleted by hand was never noticed and never came back.
    # Discord can't insert, so the only way to restore reading order is to
    # re-send everything from the gap onward.
    stored = _stored(["a", "b", "c", "d"])
    ops, deletes = ref.diff_messages(
        ["a", "b", "c", "d"], stored, missing={102}  # "c" was deleted
    )
    assert ops == [
        ("keep", 100, "a"),  # untouched prefix keeps its ids
        ("keep", 101, "b"),
        ("post", 0, "c"),  # the gap itself
        ("post", 0, "d"),  # and everything after it, to preserve order
    ]
    assert deletes == [103]  # the stale "d"; 102 is already gone


def test_diff_rebuild_starts_at_the_first_gap_only():
    stored = _stored(["a", "b", "c"])
    ops, deletes = ref.diff_messages(["a", "b", "c"], stored, missing={100, 102})
    assert [op for op, _, _ in ops] == ["post", "post", "post"]
    assert deletes == [101]  # neither missing id is re-deleted


def test_diff_gap_in_surplus_is_just_a_delete():
    # The gap sits past the rendered range: those messages are surplus the
    # delete pass removes anyway — no reason to churn the messages above it.
    stored = _stored(["a", "b", "c"])
    ops, deletes = ref.diff_messages(["a", "b"], stored, missing={102})
    assert ops == [("keep", 100, "a"), ("keep", 101, "b")]
    assert deletes == []  # 102 is already gone; nothing else is surplus


def test_diff_without_missing_ids_is_unchanged():
    ops, deletes = ref.diff_messages(["a", "b"], _stored(["a", "b"]))
    assert ops == [("keep", 100, "a"), ("keep", 101, "b")]
    assert deletes == []


# ── sweep: which tracked messages still exist ─────────────────────────


def test_missing_ids_reports_what_the_sweep_proved_absent():
    stored = _stored(["a", "b", "c"])
    assert ref.missing_message_ids(stored, {100, 102}, None) == {101}


def test_missing_ids_never_guesses_past_a_truncated_sweep():
    # A capped sweep only covered up to id 101. Calling 102 "missing" on
    # that evidence would delete and re-send real messages, so ids past the
    # horizon are assumed present.
    stored = _stored(["a", "b", "c"])
    assert ref.missing_message_ids(stored, {100}, 101) == {101}


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
        self._channel.live.discard(self._mid)


class _FakeChannel(discord.TextChannel):
    """Minimal stand-in: records sends/edits/deletes, can fail on demand.

    ``live`` is the set of ids the channel actually holds — the existence
    sweep reads it, so a test deletes a message by hand with
    ``channel.live.remove(mid)``.
    """

    def __init__(
        self, *, send_fails_at=None, edit_fails=(), live=(), history_fails=False
    ):
        self.id = 555
        self.sent, self.edits, self.deletes = [], [], []
        self.send_fails_at = send_fails_at  # nth send (0-based) raises
        self.edit_fails = set(edit_fails)
        self.live = set(live)
        self.history_fails = history_fails
        self.history_limit = None  # last limit the sweep asked for
        self.yielded = 0  # messages the sweep actually read
        self._next_id = 1000

    async def send(self, content, **kwargs):
        if self.send_fails_at is not None and len(self.sent) == self.send_fails_at:
            raise discord.HTTPException(_FakeResponse(400), "too long")
        self._next_id += 1
        self.sent.append(content)
        self.live.add(self._next_id)
        return SimpleNamespace(id=self._next_id)

    def get_partial_message(self, mid):
        return _FakePartial(self, mid)

    def history(self, *, limit=None, after=None, oldest_first=False):
        self.history_limit = limit
        floor = after.id if after is not None else 0
        ids = sorted(i for i in self.live if i > floor)[:limit]
        fails = self.history_fails

        async def _gen():
            if fails:
                raise discord.HTTPException(_FakeResponse(500), "boom")
            for i in ids:
                self.yielded += 1
                yield SimpleNamespace(id=i)

        return _gen()


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
    failing = _FakeChannel(edit_fails=[mid], live=[mid])
    result = await ref.sync_channel(ctx, _guild(failing))
    assert result["incomplete"] is True
    assert failing.edits == []  # the edit really failed

    # Next save (Discord healthy) must still see work to do.
    ok = _FakeChannel(live=[mid])
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
    live = [m for m, _ in before]
    failing = _FakeChannel(send_fails_at=0, live=live)
    result = await ref.sync_channel(ctx, _guild(failing))
    assert result["incomplete"] is True
    with open_db(db_path) as conn:
        after = ref.stored_messages(conn, GUILD)
    # All three original messages stay tracked (shifted content), nothing
    # orphaned, and the missing 4th position is retried on the next save.
    assert [m for m, _ in after] == [m for m, _ in before]

    ok = _FakeChannel(live=live)
    result = await ref.sync_channel(ctx, _guild(ok))
    assert ok.sent == ["Q3?"]  # only the missing tail is posted
    assert result["incomplete"] is False
    with open_db(db_path) as conn:
        assert len(ref.stored_messages(conn, GUILD)) == 4


# ── sync: hand-deleted messages come back ─────────────────────────────


async def _sync_ids(ctx, db_path, channel):
    await ref.sync_channel(ctx, _guild(channel))
    with open_db(db_path) as conn:
        return [m for m, _ in ref.stored_messages(conn, GUILD)]


async def test_sync_reposts_a_message_deleted_by_hand(db_path):
    # The reported bug: with the blocks config untouched every position
    # hashed "keep", sync made no Discord calls at all, and a message
    # someone deleted stayed gone however often you pressed save.
    _setup(db_path, [{"kind": "questions", "title": "SFW", "body": "Q1?\nQ2?"}])
    ctx = _Ctx(db_path)
    channel = _FakeChannel()
    ids = await _sync_ids(ctx, db_path, channel)
    assert channel.sent == ["**SFW**", "Q1?", "Q2?"]

    channel.live.remove(ids[1])  # someone deletes "Q1?" in Discord
    channel.sent.clear()

    result = await ref.sync_channel(ctx, _guild(channel))  # save, config same
    # The gap and everything after it are re-sent, so the procedure still
    # reads in order; the untouched prefix keeps its id.
    assert channel.sent == ["Q1?", "Q2?"]
    assert channel.deletes == [ids[2]]  # the now-stale copy of the tail
    assert result["repaired"] == 1 and result["incomplete"] is False
    with open_db(db_path) as conn:
        after = [m for m, _ in ref.stored_messages(conn, GUILD)]
    assert after[0] == ids[0]
    assert not set(after[1:]) & set(ids)  # rebuilt tail is all new ids


async def test_sync_leaves_an_intact_channel_alone(db_path):
    # The fast path must stay fast: nothing missing means no writes.
    _setup(db_path, [{"kind": "questions", "title": "SFW", "body": "Q1?\nQ2?"}])
    ctx = _Ctx(db_path)
    channel = _FakeChannel()
    await _sync_ids(ctx, db_path, channel)
    channel.sent.clear()

    result = await ref.sync_channel(ctx, _guild(channel))
    assert channel.sent == [] and channel.edits == [] and channel.deletes == []
    assert result["repaired"] == 0 and result["posted"] == 0


async def test_sync_skips_repair_when_it_cannot_read_history(db_path):
    # No information is not evidence of deletion — a failed sweep must never
    # trigger the destructive rebuild.
    _setup(db_path, [{"kind": "questions", "title": "SFW", "body": "Q1?\nQ2?"}])
    ctx = _Ctx(db_path)
    channel = _FakeChannel()
    ids = await _sync_ids(ctx, db_path, channel)
    channel.sent.clear()
    channel.history_fails = True

    result = await ref.sync_channel(ctx, _guild(channel))
    assert channel.sent == [] and channel.deletes == []
    assert result["incomplete"] is True  # dashboard shouldn't claim clean
    with open_db(db_path) as conn:
        assert [m for m, _ in ref.stored_messages(conn, GUILD)] == ids


async def test_sync_fills_a_freshly_repointed_channel(db_path):
    # Same root cause, different symptom: point the setting at another
    # channel and every position still hashed "keep", so the new channel
    # stayed empty. None of the tracked ids live there, so it rebuilds.
    _setup(db_path, [{"kind": "questions", "title": "SFW", "body": "Q1?\nQ2?"}])
    ctx = _Ctx(db_path)
    await _sync_ids(ctx, db_path, _FakeChannel())

    fresh = _FakeChannel()  # different channel, holds none of them
    result = await ref.sync_channel(ctx, _guild(fresh))
    assert fresh.sent == ["**SFW**", "Q1?", "Q2?"]
    assert fresh.deletes == []  # nothing of ours to clean up there
    assert result["repaired"] == 3


async def test_sync_heals_a_rebuild_that_died_mid_send(db_path):
    # A rebuild that fails partway keeps the old ids tracked for the
    # positions it never reached AND deletes those messages. That pairing
    # is what makes the next save finish the job: skipping the delete left
    # stale copies whose hashes still matched, so the next sync saw nothing
    # to do and the channel read out of order forever.
    _setup(db_path, [{"kind": "questions", "title": "", "body": "Q1?\nQ2?\nQ3?"}])
    ctx = _Ctx(db_path)
    channel = _FakeChannel()
    ids = await _sync_ids(ctx, db_path, channel)

    channel.sent.clear()  # the fake counts sends off this list
    channel.live.remove(ids[0])  # gap at the very top → full rebuild
    channel.send_fails_at = 1  # the second re-send dies

    result = await ref.sync_channel(ctx, _guild(channel))
    assert result["incomplete"] is True
    assert result["repaired"] == 1  # the gap itself did get re-sent

    # The follow-up save must actually have work to do, and must leave the
    # channel reading in order.
    channel.send_fails_at = None
    channel.sent.clear()
    result = await ref.sync_channel(ctx, _guild(channel))
    assert result["incomplete"] is False
    assert channel.sent == ["Q2?", "Q3?"]
    with open_db(db_path) as conn:
        final = [m for m, _ in ref.stored_messages(conn, GUILD)]
    assert final == sorted(final)  # ids ascend ⇒ channel is in reading order
    assert set(final) <= channel.live and len(channel.live) == 3


async def test_sync_does_not_claim_a_restore_it_never_made(db_path):
    # The gap lands past the rendered range: the message is surplus the
    # delete pass removes anyway, so nothing is restored and the panel must
    # not print "1 restored".
    _setup(db_path, [{"kind": "questions", "title": "", "body": "Q1?\nQ2?\nQ3?"}])
    ctx = _Ctx(db_path)
    channel = _FakeChannel()
    ids = await _sync_ids(ctx, db_path, channel)

    channel.sent.clear()
    channel.live.remove(ids[2])  # deleted in Discord...
    _setup(db_path, [{"kind": "questions", "title": "", "body": "Q1?\nQ2?"}])  # ...and cut

    result = await ref.sync_channel(ctx, _guild(channel))
    assert result["repaired"] == 0
    assert channel.sent == [] and channel.deletes == []


async def test_sync_flags_a_sweep_that_ran_out_of_history(monkeypatch, db_path):
    # Past the cap the sweep assumes the unread ids are present (right), but
    # it must not report a clean sync — a deletion in the unread tail goes
    # unnoticed until someone looks.
    _setup(db_path, [{"kind": "questions", "title": "", "body": "Q1?\nQ2?\nQ3?"}])
    ctx = _Ctx(db_path)
    channel = _FakeChannel()
    ids = await _sync_ids(ctx, db_path, channel)

    channel.sent.clear()
    channel.live.remove(ids[0])  # forces the sweep to read the whole window
    monkeypatch.setattr(ref, "_SWEEP_LIMIT", 1)

    result = await ref.sync_channel(ctx, _guild(channel))
    assert result["incomplete"] is True
    assert channel.history_limit == 1


async def test_sweep_stops_once_every_tracked_message_is_found(db_path):
    # The fast path shouldn't read the whole channel: with nothing missing
    # the sweep bails at the last tracked message rather than trawling
    # whatever humans posted afterwards.
    _setup(db_path, [{"kind": "questions", "title": "", "body": "Q1?\nQ2?"}])
    ctx = _Ctx(db_path)
    channel = _FakeChannel()
    ids = await _sync_ids(ctx, db_path, channel)
    channel.live.update({max(ids) + 1, max(ids) + 2})  # human chatter after

    swept = await ref._sweep_existing(channel, [(m, "") for m in ids])
    assert swept == (set(ids), None)  # conclusive: nothing missing
    assert channel.yielded == len(ids)  # the chatter was never read


def test_blocks_from_messages_drafts_text_blocks():
    drafts = ref.blocks_from_messages(["First rule post", "", "  ", "Questions wall"])
    assert drafts == [
        {"kind": "text", "title": "", "body": "First rule post"},
        {"kind": "text", "title": "", "body": "Questions wall"},
    ]
