"""Tests for games_external.logic — the multi-watch collector config (#70/#65)."""

from __future__ import annotations

import pytest

from bot_modules.games_external import logic
from bot_modules.services.games_db import GamesDb
from tests.db_template import migrated_db

GUILD = 111
CHAN_A, CHAN_B = 201, 202
GAMEBOT, CATBOT = 620307267241377793, 966695034340663367


@pytest.fixture
def gdb(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return GamesDb(db_path)


@pytest.mark.asyncio
async def test_set_and_get_watch_carries_kind(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot", set_by=9)
    row = await logic.get_watch_for_bot(gdb, GUILD, GAMEBOT)
    assert row is not None
    assert row["channel_id"] == CHAN_A
    assert row["kind"] == "gamebot"
    assert row["enabled"] == 1


@pytest.mark.asyncio
async def test_multiple_bots_coexist_per_guild(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot", set_by=9)
    await logic.set_watch(gdb, GUILD, CHAN_B, CATBOT, "catbot", set_by=9)

    watches = await logic.list_watches(gdb, GUILD)
    by_bot = {int(w["bot_user_id"]): w for w in watches}
    assert set(by_bot) == {GAMEBOT, CATBOT}
    assert by_bot[CATBOT]["kind"] == "catbot"
    assert by_bot[GAMEBOT]["kind"] == "gamebot"


@pytest.mark.asyncio
async def test_one_bot_can_be_watched_in_several_channels(gdb):
    # Migration 135: a watch is a (bot, channel) pair, so pointing the same bot
    # at a second channel *adds* it rather than moving it off the first. Before
    # this, every game the bot ran outside its one watched channel was dropped.
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot", set_by=9)
    await logic.set_watch(gdb, GUILD, CHAN_B, GAMEBOT, "gamebot", set_by=9)

    watches = await logic.watch_channels_for_bot(gdb, GUILD, GAMEBOT)
    assert {int(w["channel_id"]) for w in watches} == {CHAN_A, CHAN_B}


@pytest.mark.asyncio
async def test_re_watching_the_same_channel_updates_in_place(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot", set_by=9)
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "catbot", set_by=9)

    watches = await logic.list_watches(gdb, GUILD)
    assert len(watches) == 1
    assert watches[0]["kind"] == "catbot"  # updated, not duplicated


@pytest.mark.asyncio
async def test_disable_covers_every_channel_a_bot_plays_in(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot", set_by=9)
    await logic.set_watch(gdb, GUILD, CHAN_B, GAMEBOT, "gamebot", set_by=9)

    assert await logic.set_watch_enabled(gdb, GUILD, GAMEBOT, False) is True
    assert await logic.load_all_watches(gdb) == []

    # …and a single channel can be toggled on its own.
    assert await logic.set_watch_enabled(gdb, GUILD, GAMEBOT, True, CHAN_A) is True
    live = {int(r["channel_id"]) for r in await logic.load_all_watches(gdb)}
    assert live == {CHAN_A}


@pytest.mark.asyncio
async def test_enable_is_per_bot(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot", set_by=9)
    await logic.set_watch(gdb, GUILD, CHAN_B, CATBOT, "catbot", set_by=9)

    assert await logic.set_watch_enabled(gdb, GUILD, GAMEBOT, False) is True
    # Missing bot toggles nothing.
    assert await logic.set_watch_enabled(gdb, GUILD, 999, False) is False

    enabled = {int(r["bot_user_id"]) for r in await logic.load_all_watches(gdb)}
    assert enabled == {CATBOT}  # only the still-enabled bot warms the cache


@pytest.mark.asyncio
async def test_count_messages_filters_by_bot(gdb):
    for mid, bot in ((1, GAMEBOT), (2, GAMEBOT), (3, CATBOT)):
        await gdb.execute(
            "INSERT INTO games_external_messages "
            "(message_id, guild_id, channel_id, author_id, created_at) "
            "VALUES (?, ?, ?, ?, '2026-07-21T00:00:00')",
            (mid, GUILD, CHAN_A, bot),
        )
    assert await logic.count_messages(gdb, GUILD) == 3
    assert await logic.count_messages(gdb, GUILD, GAMEBOT) == 2
    assert await logic.count_messages(gdb, GUILD, CATBOT) == 1


def test_valid_kinds_expose_labels():
    assert "gamebot" in logic.VALID_WATCH_KINDS
    assert "catbot" in logic.VALID_WATCH_KINDS
    assert logic.WATCH_KIND_LABELS["catbot"] == "Cat Bot"


@pytest.mark.asyncio
async def test_claim_payout_is_first_only(gdb):
    assert await logic.claim_payout(gdb, 555, GUILD, "gamebot_cah") is True
    # A second claim on the same terminal message never re-pays.
    assert await logic.claim_payout(gdb, 555, GUILD, "gamebot_cah") is False


@pytest.mark.asyncio
async def test_recent_channel_messages_oldest_first_and_scoped(gdb):
    async def bank(mid, chan, bot, ts):
        await gdb.execute(
            "INSERT INTO games_external_messages "
            "(message_id, guild_id, channel_id, author_id, created_at, embeds_json) "
            "VALUES (?, ?, ?, ?, ?, '[]')",
            (mid, GUILD, chan, bot, ts),
        )

    await bank(1, CHAN_A, GAMEBOT, "2026-07-21T01:00:00")
    await bank(2, CHAN_A, GAMEBOT, "2026-07-21T01:00:05")
    await bank(3, CHAN_A, CATBOT, "2026-07-21T01:00:06")   # other bot
    await bank(4, CHAN_B, GAMEBOT, "2026-07-21T01:00:07")  # other channel
    await bank(5, CHAN_A, GAMEBOT, "2026-07-21T01:00:09")  # after the cutoff

    rows = await logic.recent_channel_messages(
        gdb, GUILD, CHAN_A, GAMEBOT, "2026-07-21T01:00:05"
    )
    assert [int(r["message_id"]) for r in rows] == [1, 2]  # scoped + oldest-first


# ── parse-buffer retention (2026-08 review, games batch-bc A1) ────────


@pytest.mark.asyncio
async def test_sweep_old_buffer_rows_deletes_only_old_rows(gdb):
    from bot_modules.games_external.logic import sweep_old_buffer_rows

    await gdb.execute(
        "INSERT INTO games_external_messages (message_id, guild_id, channel_id, "
        "author_id, created_at, content, collected_at) "
        "VALUES (1, 9, 1, 2, 'x', 'old', datetime('now', '-40 days')), "
        "       (2, 9, 1, 2, 'x', 'new', datetime('now', '-1 days'))",
    )
    removed = await sweep_old_buffer_rows(gdb)
    rows = await gdb.fetchall("SELECT message_id FROM games_external_messages")
    assert removed == 1
    assert [r["message_id"] for r in rows] == [2]


# ── a long game's window pages back to its lobby (photo-external-110) ────────


def _lobby(joined):
    return [{"title": "host is starting a Cards Against Humanity game!",
             "fields": [{"name": "Players (2/12)", "value": ", ".join(f"<@{u}>" for u in joined)}]}]


def _standings(scores, title="Round winner"):
    return [{"title": title, "fields": [{"name": "Standings",
             "value": "\n".join(f"<@{u}>: **{n}**" for u, n in scores.items())}]}]


def _chatter(i):
    return [{"title": "Play your card", "description": f"round {i}"}]


async def _bank_long_game(gdb, *, length: int, prior_terminal: bool = False):
    """A game of ``length`` banked messages: lobby first, a *Final scores*
    last, chatter between. Timestamps are one second apart from a fixed base."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 8, 30, 20, 0, 0, tzinfo=timezone.utc)

    async def bank(mid, offset, embeds):
        import json
        await gdb.execute(
            "INSERT INTO games_external_messages "
            "(message_id, guild_id, channel_id, author_id, created_at, embeds_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, GUILD, CHAN_A, GAMEBOT, (base + timedelta(seconds=offset)).isoformat(),
             json.dumps(embeds)),
        )

    if prior_terminal:
        await bank(1, -5, _standings({7: 5}, title="Final scores"))
    await bank(100, 0, _lobby([11, 22]))
    for i in range(1, length - 1):
        await bank(100 + i, i, _chatter(i))
    await bank(100 + length - 1, length - 1, _standings({11: 5, 22: 3}, title="Final scores"))
    return 100 + length - 1, (base + timedelta(seconds=length - 1)).isoformat()


@pytest.mark.asyncio
async def test_a_400_message_game_keeps_its_lobby(gdb):
    # 300 rows was the whole lookback: a game longer than that lost its lobby,
    # and with it the host bounty and the sub-game identification.
    over_id, over_at = await _bank_long_game(gdb, length=400)
    window = await logic.game_window_rows(gdb, GUILD, CHAN_A, GAMEBOT, over_id, over_at)
    assert len(window) == 400
    assert window[0]["message_id"] == 100  # the lobby
    assert window[-1]["message_id"] == over_id


@pytest.mark.asyncio
async def test_window_paging_stops_at_the_previous_terminal(gdb):
    over_id, over_at = await _bank_long_game(gdb, length=350, prior_terminal=True)
    window = await logic.game_window_rows(gdb, GUILD, CHAN_A, GAMEBOT, over_id, over_at)
    assert window[0]["message_id"] == 100
    assert 1 not in {r["message_id"] for r in window}


@pytest.mark.asyncio
async def test_window_paging_is_capped(gdb):
    # A channel with no lobby and no terminal anywhere behind the finish must
    # not read the whole buffer: the cap bounds the work, and the window is
    # whatever those rows hold.
    over_id, over_at = await _bank_long_game(gdb, length=120)
    window = await logic.game_window_rows(
        gdb, GUILD, CHAN_A, GAMEBOT, over_id, over_at, page=25, max_rows=60,
    )
    assert 25 < len(window) <= 60
    assert window[-1]["message_id"] == over_id


# ── health signal: last payout + unpaid finishes (photo-external-100) ────────


@pytest.mark.asyncio
async def test_last_payout_is_scoped_to_the_watch_kind_and_channel(gdb):
    import json

    async def bank(mid, chan):
        await gdb.execute(
            "INSERT INTO games_external_messages (message_id, guild_id, channel_id, "
            "author_id, created_at, embeds_json) VALUES (?, ?, ?, ?, '2026-08-30T20:00:00', ?)",
            (mid, GUILD, chan, GAMEBOT, json.dumps([])),
        )

    await bank(1, CHAN_A)
    await bank(2, CHAN_B)
    await gdb.execute(
        "INSERT INTO games_external_payouts (message_id, guild_id, kind, paid_at) VALUES "
        "(1, ?, 'gamebot_cah', '2026-08-30 20:01:00'), "
        "(2, ?, 'gamebot_anagrams', '2026-08-31 20:01:00'), "
        "(3, ?, 'catbot', '2026-09-01 20:01:00')",
        (GUILD, GUILD, GUILD),
    )
    assert await logic.last_payout_at(gdb, GUILD, CHAN_A, "gamebot") == "2026-08-30 20:01:00"
    assert await logic.last_payout_at(gdb, GUILD, CHAN_B, "gamebot") == "2026-08-31 20:01:00"
    # A claim whose message has left the buffer (or was never a message id —
    # Co-ordle keys on the round) counts for every channel of that kind.
    assert await logic.last_payout_at(gdb, GUILD, CHAN_A, "catbot") == "2026-09-01 20:01:00"
    assert await logic.last_payout_at(gdb, GUILD, CHAN_A, "wordle") is None


@pytest.mark.asyncio
async def test_unpaid_finishes_counts_payable_windows_with_no_claim(gdb):
    import json

    async def bank(mid, ts, embeds, chan=CHAN_A):
        await gdb.execute(
            "INSERT INTO games_external_messages (message_id, guild_id, channel_id, "
            "author_id, created_at, embeds_json) VALUES (?, ?, ?, ?, ?, ?)",
            (mid, GUILD, chan, GAMEBOT, ts, json.dumps(embeds)),
        )

    now = "2026-09-01T12:00:00+00:00"
    # game 1: paid
    await bank(10, "2026-08-30T20:00:00+00:00", _lobby([11, 22]))
    await bank(11, "2026-08-30T20:10:00+00:00", _standings({11: 5, 22: 1}, title="Final scores"))
    # game 2: a parser miss — no claim
    await bank(20, "2026-08-31T20:00:00+00:00", _lobby([11, 22]))
    await bank(21, "2026-08-31T20:10:00+00:00", _standings({11: 5, 22: 2}, title="Final scores"))
    # game 3: unparsed (Chess) — not counted
    await bank(30, "2026-08-31T21:00:00+00:00",
               [{"title": "host is starting a Chess game!"}])
    await bank(31, "2026-08-31T21:10:00+00:00", [{"title": "Game over!", "description": "gg"}])
    # game 4: older than the window — not counted
    await bank(40, "2026-07-01T20:00:00+00:00", _lobby([11, 22]))
    await bank(41, "2026-07-01T20:10:00+00:00", _standings({11: 5}, title="Final scores"))
    await gdb.execute(
        "INSERT INTO games_external_payouts (message_id, guild_id, kind) VALUES (11, ?, 'gamebot_cah')",
        (GUILD,),
    )
    unpaid = await logic.unpaid_finishes(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot", now_iso=now)
    assert unpaid == [21]


@pytest.mark.asyncio
async def test_unpaid_finishes_for_the_one_message_kinds(gdb):
    async def bank(mid, ts, content, kind_bot):
        await gdb.execute(
            "INSERT INTO games_external_messages (message_id, guild_id, channel_id, "
            "author_id, created_at, content, embeds_json) VALUES (?, ?, ?, ?, ?, ?, '[]')",
            (mid, GUILD, CHAN_A, kind_bot, ts, content),
        )

    now = "2026-09-01T12:00:00+00:00"
    await bank(1, "2026-08-31T10:00:00+00:00", "alice cought <:wildcat:1> Wild cat!", CATBOT)
    await bank(2, "2026-08-31T11:00:00+00:00", "A cat has appeared!", CATBOT)  # a spawn
    await bank(3, "2026-08-31T12:00:00+00:00", "bob cought <:finecat:1> Fine cat!", CATBOT)
    await gdb.execute(
        "INSERT INTO games_external_payouts (message_id, guild_id, kind) VALUES (1, ?, 'catbot')",
        (GUILD,),
    )
    assert await logic.unpaid_finishes(gdb, GUILD, CHAN_A, CATBOT, "catbot", now_iso=now) == [3]
    wordle_bot = 5
    await bank(7, "2026-08-31T13:00:00+00:00",
               "**Your group is on a 9 day streak!** Here are yesterday's results:\n👑 3/6: <@11>",
               wordle_bot)
    await bank(8, "2026-08-31T14:00:00+00:00", "<@11> is playing", wordle_bot)
    assert await logic.unpaid_finishes(gdb, GUILD, CHAN_A, wordle_bot, "wordle", now_iso=now) == [7]
