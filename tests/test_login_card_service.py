"""Tests for services/login_card_service.py — the live-updating login card.

The card is a DM edited in place all day. Three things make that safe rather
than spammy, and each has a test here: it only ever edits a message the bot
really sent as a DM, it skips the API call when nothing the member can see
moved, and it stops — at completion, at the day roll, or when the member
deletes the message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.services.embeds import DM_PRIMARY
from bot_modules.services.economy_service import set_notify_muted
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_service import (
    DEFAULT_ECON_SETTINGS,
    DmDelivery,
    LoginOutcome,
    save_econ_settings,
)
from bot_modules.services.login_card_service import (
    all_personal_done,
    card_handle,
    build_login_embed,
    card_signature,
    drop_stale_cards,
    due_cards,
    forget_card,
    mark_card,
    record_card,
    refresh_guild_cards,
)
from tests.db_template import migrated_db

GUILD = 123
USER = 1001
OTHER = 1002
DM_CHANNEL = 555
MESSAGE = 999
GAME_ROLE = 4242

# The sweep derives the guild's local day from the timestamp it is handed, so
# the test days are derived the same way rather than hard-coded — otherwise a
# card written for "today" is invisible to a tick run at epoch 1000.
NOW = 1756500000.0        # 2026-08-29, comfortably mid-day in any offset
LATER = NOW + 3600.0


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


@pytest.fixture
def today(db):
    with open_db(db) as conn:
        return local_day_for(NOW, get_tz_offset_hours(conn, GUILD))


@pytest.fixture
def yesterday(db):
    with open_db(db) as conn:
        return local_day_for(NOW - 86400.0, get_tz_offset_hours(conn, GUILD))


def _outcome(**kw) -> LoginOutcome:
    base = dict(
        paid=5, streak=12, milestone=0,
        grace_consumed=False, reset=False, shield_consumed=False,
    )
    base.update(kw)
    return LoginOutcome(**base)


def _quest(title: str, state: str, qtype: str = "daily", **kw) -> dict:
    q = {"title": title, "qtype": qtype, "state": state}
    q.update(kw)
    return q


def _store(db, *, user_id=USER, local_day, signature="sig", final=False):
    with open_db(db) as conn:
        record_card(
            conn, GUILD, user_id,
            local_day=local_day,
            dm_channel_id=DM_CHANNEL,
            message_id=MESSAGE,
            signature=signature,
            outcome=_outcome(),
            prior_streak=11,
            final=final,
            now_ts=1000.0,
        )


# ── all_personal_done: what counts as "finished for the day" ──────────


@pytest.mark.parametrize(
    "quests, expected",
    [
        pytest.param([], True, id="no-quests-at-all"),
        pytest.param([_quest("A", "done")], True, id="only-quest-done"),
        pytest.param([_quest("A", "claimable")], False, id="one-still-open"),
        pytest.param(
            [_quest("A", "done"), _quest("B", "message_count")], False,
            id="one-of-two-open",
        ),
        pytest.param(
            [_quest("A", "done"), _quest("B", "done", qtype="weekly")], True,
            id="daily-and-weekly-both-done",
        ),
        # Community and monthly goals are guild-wide counters that never reach
        # "done" for one member. If they counted, no card would ever finish.
        pytest.param(
            [_quest("A", "done"), _quest("Goal", "community", qtype="community")],
            True, id="community-goal-does-not-block",
        ),
        pytest.param(
            [_quest("A", "done"), _quest("Goal", "community", qtype="monthly")],
            True, id="monthly-goal-does-not-block",
        ),
        # Event quests are standing payouts with no period — likewise not the
        # member's checklist.
        pytest.param(
            [_quest("A", "done"), _quest("Photo", "photo_post", qtype="event")],
            True, id="event-quest-does-not-block",
        ),
    ],
)
def test_all_personal_done(quests, expected):
    assert all_personal_done(quests) is expected


# ── card_handle: only a real DM is ever remembered ────────────────────


def test_card_handle_takes_the_dm_channel_and_message():
    msg = MagicMock(spec=discord.Message)
    msg.id = MESSAGE
    msg.channel = MagicMock(id=DM_CHANNEL)
    assert card_handle(DmDelivery("dm", msg)) == (DM_CHANNEL, MESSAGE)


def test_card_handle_refuses_the_public_bank_fallback():
    """The bank-channel copy is a different, wellness-free embed. Storing it
    as a card would later edit the private render into a public channel."""
    msg = MagicMock(spec=discord.Message)
    msg.id = MESSAGE
    msg.channel = MagicMock(id=777)
    assert card_handle(DmDelivery("bank", msg)) is None


@pytest.mark.parametrize("surface", ["dropped", "failed"])
def test_card_handle_refuses_a_delivery_that_sent_nothing(surface):
    """Muted, not opted in, DMs closed — all reported "handled" as a bool, and
    all have no message to edit."""
    assert card_handle(DmDelivery(surface)) is None


# ── card_signature: the thing that keeps this cheap ───────────────────


def _embed(quests, wellness=None):
    return build_login_embed(
        DEFAULT_ECON_SETTINGS, _outcome(), 11, quests, [],
        discord.Color.blurple(), wellness_value=wellness,
    )


def test_signature_is_stable_for_identical_state():
    a = _embed([_quest("A", "message_count", progress_current=3, progress_target=10)])
    b = _embed([_quest("A", "message_count", progress_current=3, progress_target=10)])
    assert card_signature(a) == card_signature(b)


def test_signature_moves_when_progress_moves():
    before = _embed([_quest("A", "message_count", progress_current=3, progress_target=10)])
    after = _embed([_quest("A", "message_count", progress_current=7, progress_target=10)])
    assert card_signature(before) != card_signature(after)


def test_signature_moves_when_the_wellness_section_goes():
    """Wellness is recomputed live, so a member who pauses at noon must see
    the section leave the card — the signature has to notice that, not just
    quest bars."""
    with_section = _embed([_quest("A", "claimable")], wellness="Day 4 🌿")
    without = _embed([_quest("A", "claimable")], wellness=None)
    assert card_signature(with_section) != card_signature(without)


# ── the card store ────────────────────────────────────────────────────


def test_record_and_read_back_a_card(db, today):
    _store(db, local_day=today)
    with open_db(db) as conn:
        rows = due_cards(conn, GUILD, today)
    assert len(rows) == 1
    assert int(rows[0]["message_id"]) == MESSAGE
    assert int(rows[0]["dm_channel_id"]) == DM_CHANNEL
    assert int(rows[0]["prior_streak"]) == 11


def test_one_row_per_member_so_the_table_self_prunes(db, today, yesterday):
    """Tomorrow's digest overwrites today's handle rather than piling up."""
    _store(db, local_day=yesterday)
    _store(db, local_day=today)
    with open_db(db) as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM econ_login_digest_cards"
        ).fetchone()["c"]
    assert total == 1


def test_final_cards_are_not_due(db, today):
    _store(db, local_day=today, final=True)
    with open_db(db) as conn:
        assert due_cards(conn, GUILD, today) == []


def test_yesterdays_card_is_dropped_not_edited(db, today, yesterday):
    """A card must never outlive the day it describes: rewriting a day-old
    message with today's numbers would rewrite history in someone's DMs."""
    _store(db, local_day=yesterday)
    with open_db(db) as conn:
        assert due_cards(conn, GUILD, today) == []
        assert drop_stale_cards(conn, GUILD, today) == 1
        assert due_cards(conn, GUILD, yesterday) == []


def test_mark_and_forget(db, today):
    _store(db, local_day=today)
    with open_db(db) as conn:
        mark_card(
            conn, GUILD, USER, local_day=today,
            signature="new", final=True, now_ts=2000.0,
        )
        row = conn.execute(
            "SELECT * FROM econ_login_digest_cards WHERE user_id = ?", (USER,)
        ).fetchone()
        assert row["signature"] == "new"
        assert row["final"] == 1
        forget_card(conn, GUILD, USER, local_day=today)
        assert conn.execute(
            "SELECT COUNT(*) c FROM econ_login_digest_cards"
        ).fetchone()["c"] == 0


# ── the sweep ─────────────────────────────────────────────────────────


def _bot(partial, *, role_ids=(GAME_ROLE,), member_present=True):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.name = "Test Guild"
    guild.icon = None
    member = None
    if member_present:
        member = MagicMock()
        member.roles = [MagicMock(id=rid) for rid in role_ids]
    guild.get_member.return_value = member
    bot.get_guild.return_value = guild
    messageable = MagicMock()
    messageable.get_partial_message.return_value = partial
    bot.get_partial_messageable.return_value = messageable
    return bot


def _partial(edit_error: Exception | None = None):
    partial = MagicMock()
    partial.edit = AsyncMock(side_effect=edit_error)
    return partial


def _enable(db, **extra):
    with open_db(db) as conn:
        save_econ_settings(
            conn, GUILD, {"enabled": True, "game_role_id": GAME_ROLE, **extra}
        )


def _open_board(monkeypatch, quests):
    """Give the sweep a quest board.

    A fresh test db has no quests at all, so every board loads empty and every
    card is vacuously final after one render. That would let the signature-skip
    test pass because the row left the sweep, not because the signature
    matched. Board assembly is covered in economy_quests_service's own tests;
    here it only needs to be non-empty.
    """
    monkeypatch.setattr(
        "bot_modules.services.login_card_service.load_member_quest_board",
        lambda conn, settings, gid, uid, day: list(quests),
    )


async def test_sweep_edits_a_card_whose_content_moved(db, today):
    _enable(db)
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    edited = await refresh_guild_cards(_bot(partial), db, GUILD, NOW)
    assert edited == 1
    partial.edit.assert_awaited_once()
    assert "embed" in partial.edit.await_args.kwargs


async def test_sweep_skips_the_api_call_when_nothing_changed(db, today, monkeypatch):
    """An hour in which a member did nothing must cost zero requests — this is
    what makes an hourly pass over every member affordable.

    The member still has an open quest here, so the row stays in the sweep:
    the second pass is skipped because the signature matched, not because the
    card had finished.
    """
    _enable(db)
    _open_board(monkeypatch, [_quest("Open", "message_count",
                                     progress_current=3, progress_target=10)])
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    bot = _bot(partial)
    # First pass renders and stores the real signature…
    await refresh_guild_cards(bot, db, GUILD, NOW)
    partial.edit.reset_mock()
    with open_db(db) as conn:
        row = due_cards(conn, GUILD, today)[0]
    assert row["final"] == 0  # still live, so the skip below is a real skip
    # …the second finds it unchanged.
    edited = await refresh_guild_cards(bot, db, GUILD, LATER)
    assert edited == 0
    partial.edit.assert_not_awaited()


async def test_sweep_edits_again_when_progress_moves(db, today, monkeypatch):
    """The whole point: a bar that moved reaches the member's card."""
    _enable(db)
    board = [_quest("Open", "message_count", progress_current=3, progress_target=10)]
    _open_board(monkeypatch, board)
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    bot = _bot(partial)
    assert await refresh_guild_cards(bot, db, GUILD, NOW) == 1
    board[0]["progress_current"] = 7
    partial.edit.reset_mock()
    assert await refresh_guild_cards(bot, db, GUILD, LATER) == 1
    resent = partial.edit.await_args.kwargs["embed"]
    assert any("7 / 10" in f.value for f in resent.fields)


async def test_sweep_keeps_working_while_a_quest_is_open(db, today, monkeypatch):
    """A member mid-way through their day stays in the sweep."""
    _enable(db)
    _open_board(monkeypatch, [
        _quest("Done", "done"),
        _quest("Open", "message_count", progress_current=1, progress_target=10),
    ])
    _store(db, local_day=today, signature="stale")
    await refresh_guild_cards(_bot(_partial()), db, GUILD, NOW)
    with open_db(db) as conn:
        assert len(due_cards(conn, GUILD, today)) == 1


async def test_sweep_stops_once_the_last_open_quest_is_done(db, today, monkeypatch):
    """The finished card gets one last render, then leaves the sweep — the
    community bars on it freeze at that point, which is the deal every
    finished member gets."""
    _enable(db)
    board = [_quest("Open", "message_count", progress_current=9, progress_target=10)]
    _open_board(monkeypatch, board)
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    bot = _bot(partial)
    await refresh_guild_cards(bot, db, GUILD, NOW)
    with open_db(db) as conn:
        assert len(due_cards(conn, GUILD, today)) == 1
    board[0] = _quest("Open", "done", progress_current=10, progress_target=10)
    partial.edit.reset_mock()
    assert await refresh_guild_cards(bot, db, GUILD, LATER) == 1
    final_embed = partial.edit.await_args.kwargs["embed"]
    # The finished quest is still on the card, ticked — not silently dropped.
    assert any("✅ **Open**" in f.value for f in final_embed.fields)
    with open_db(db) as conn:
        assert due_cards(conn, GUILD, today) == []


async def test_sweep_edits_once_more_then_stops_when_everything_is_done(db, today):
    """With no personal quests open the card is final: one last render, then
    the row leaves the sweep for good."""
    _enable(db)
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    bot = _bot(partial)
    assert await refresh_guild_cards(bot, db, GUILD, NOW) == 1
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT final FROM econ_login_digest_cards WHERE user_id = ?", (USER,)
        ).fetchone()["final"] == 1
        assert due_cards(conn, GUILD, today) == []
    partial.edit.reset_mock()
    assert await refresh_guild_cards(bot, db, GUILD, LATER) == 0
    partial.edit.assert_not_awaited()


async def test_sweep_forgets_a_deleted_message_and_never_reposts(db, today):
    """The card is silent by design. A member who deleted the DM must not get
    a fresh one — that would notify them precisely because they opted out."""
    _enable(db)
    _store(db, local_day=today, signature="stale")
    partial = _partial(discord.NotFound(MagicMock(status=404), "gone"))
    bot = _bot(partial)
    assert await refresh_guild_cards(bot, db, GUILD, NOW) == 0
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM econ_login_digest_cards"
        ).fetchone()["c"] == 0
    # Nothing was sent to replace it.
    bot.get_partial_messageable.return_value.send.assert_not_called()


async def test_sweep_keeps_the_row_after_a_transient_failure(db, today):
    """A 500 is not a reason to give up on the day — try again next hour."""
    _enable(db)
    _store(db, local_day=today, signature="stale")
    partial = _partial(discord.HTTPException(MagicMock(status=500), "boom"))
    assert await refresh_guild_cards(_bot(partial), db, GUILD, NOW) == 0
    with open_db(db) as conn:
        rows = due_cards(conn, GUILD, today)
    assert len(rows) == 1
    assert rows[0]["signature"] == "stale"  # untouched, so next hour retries


async def test_sweep_does_nothing_when_the_dial_is_off(db, today):
    """The dashboard switch has to actually stop the edits, not just hide a
    setting — a preference that isn't enforced is worse than none."""
    _enable(db, login_card_live_updates=False)
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    assert await refresh_guild_cards(_bot(partial), db, GUILD, NOW) == 0
    partial.edit.assert_not_awaited()


async def test_sweep_does_nothing_when_the_economy_is_off(db, today):
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    assert await refresh_guild_cards(_bot(partial), db, GUILD, NOW) == 0
    partial.edit.assert_not_awaited()


async def test_sweep_drops_yesterdays_rows_without_editing_them(db, today, yesterday):
    _enable(db)
    _store(db, local_day=yesterday, signature="stale")
    partial = _partial()
    assert await refresh_guild_cards(_bot(partial), db, GUILD, NOW) == 0
    partial.edit.assert_not_awaited()
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM econ_login_digest_cards"
        ).fetchone()["c"] == 0


async def test_sweep_edits_a_private_dm_channel_not_a_guild_channel(db, today):
    """The handle must resolve as a DM. The public bank-channel fallback is a
    deliberately different embed, and editing it would publish the wellness
    section."""
    _enable(db)
    _store(db, local_day=today, signature="stale")
    bot = _bot(_partial())
    await refresh_guild_cards(bot, db, GUILD, NOW)
    args, kwargs = bot.get_partial_messageable.call_args
    assert args[0] == DM_CHANNEL
    assert kwargs["type"] is discord.ChannelType.private


# ── regressions found in review ───────────────────────────────────────


async def test_refreshed_card_keeps_the_dm_branding(db, today, monkeypatch):
    """The send path brands the embed (attribution footer + the DM accent) on
    its way out; a refresh that re-renders from scratch has to apply the same
    branding or the card silently loses its footer — and, in a guild with no
    accent configured, changes colour — at the first edit."""
    _enable(db)
    _open_board(monkeypatch, [_quest("Open", "message_count",
                                     progress_current=3, progress_target=10)])
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    await refresh_guild_cards(_bot(partial), db, GUILD, NOW)
    sent = partial.edit.await_args.kwargs["embed"]
    assert sent.footer.text == "Test Guild"
    # DM_PRIMARY, not the generic embed default: an unbranded guild keeps the
    # look every other economy DM has.
    assert sent.color == discord.Color(DM_PRIMARY)


def test_mark_card_will_not_stamp_over_another_day(db, today, yesterday):
    """The sweep takes its day once and then does Discord I/O per member. If
    the day rolls mid-pass and that member logs in again, their fresh row must
    not be stamped with the finished flag from yesterday's card."""
    _store(db, local_day=today, signature="fresh")
    with open_db(db) as conn:
        mark_card(
            conn, GUILD, USER, local_day=yesterday,
            signature="stale", final=True, now_ts=2000.0,
        )
        row = conn.execute(
            "SELECT * FROM econ_login_digest_cards WHERE user_id = ?", (USER,)
        ).fetchone()
    assert row["signature"] == "fresh"
    assert row["final"] == 0


def test_forget_card_will_not_delete_another_day(db, today, yesterday):
    """A 404 for yesterday's message must not throw away a card sent minutes
    ago."""
    _store(db, local_day=today)
    with open_db(db) as conn:
        forget_card(conn, GUILD, USER, local_day=yesterday)
        assert len(due_cards(conn, GUILD, today)) == 1


async def test_sweep_stops_for_a_member_who_muted_economy_dms(db, today, monkeypatch):
    """Muting is a stored preference, and the sweep is a second path writing
    to the same message — "the edit is silent anyway" is not ours to decide."""
    _enable(db)
    _open_board(monkeypatch, [_quest("Open", "message_count",
                                     progress_current=3, progress_target=10)])
    _store(db, local_day=today, signature="stale")
    with open_db(db) as conn:
        set_notify_muted(conn, GUILD, USER, True)
    partial = _partial()
    assert await refresh_guild_cards(_bot(partial), db, GUILD, NOW) == 0
    partial.edit.assert_not_awaited()
    with open_db(db) as conn:
        assert due_cards(conn, GUILD, today) == []


async def test_sweep_stops_for_a_member_who_lost_the_opt_in_role(db, today, monkeypatch):
    """The send path gates on the opt-in role; dropping it mid-day has to stop
    the refresh too, or the opt-out only half works."""
    _enable(db)
    _open_board(monkeypatch, [_quest("Open", "message_count",
                                     progress_current=3, progress_target=10)])
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    bot = _bot(partial, role_ids=())  # role removed since this morning
    assert await refresh_guild_cards(bot, db, GUILD, NOW) == 0
    partial.edit.assert_not_awaited()
    with open_db(db) as conn:
        assert due_cards(conn, GUILD, today) == []


async def test_sweep_stops_for_a_member_who_left(db, today, monkeypatch):
    _enable(db)
    _open_board(monkeypatch, [_quest("Open", "message_count",
                                     progress_current=3, progress_target=10)])
    _store(db, local_day=today, signature="stale")
    partial = _partial()
    bot = _bot(partial, member_present=False)
    assert await refresh_guild_cards(bot, db, GUILD, NOW) == 0
    partial.edit.assert_not_awaited()
