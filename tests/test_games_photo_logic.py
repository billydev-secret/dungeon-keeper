"""Tests for the Photo Challenge bank-pull helper.

Photo Challenge prompts live in the DB question bank (``games_question_bank``,
``game_type='photo'``) and are curated on the dashboard's Photo Challenge
page — there is no static prompt bank. The only standalone pure logic is
``get_photo_prompt`` (bank-only, no AI fallback), exercised here against a
fake db. Questions carry a JSON ``tags`` array; the reserved ``nsfw`` tag is
excluded unless the caller passes ``allow_nsfw=True`` (driven by the Discord
channel's age-restriction flag — a requested tag cannot re-enable it). The
card rendering and thread flow reuse the same helpers covered by the
FFA/confessions tests.
"""

from __future__ import annotations

import asyncio
import json

from bot_modules.games.utils.question_source import get_photo_prompt
from bot_modules.games_photo.logic import BACKFILL_WINDOW_SECONDS, backfill_card_counts
from bot_modules.services.games_db import GamesDb


class _FakeDB:
    """Minimal async db stub matching the fetchall/execute surface used by
    ``_get_bank_question`` (selects question_id, question_text, tags,
    last_served_at; marks the served row via execute)."""

    def __init__(self, rows: list[tuple[str, list[str], str]]):
        # rows: (game_type, tags_list, question_text)
        self._rows = rows
        self.served: list[int] = []

    async def fetchall(self, sql: str, params: tuple):
        (game_type,) = params
        return [
            (qid, r[2], json.dumps(r[1]), None)
            for qid, r in enumerate(self._rows)
            if r[0] == game_type
        ]

    async def execute(self, sql: str, params: tuple):
        (qid,) = params
        self.served.append(qid)


def _run(coro):
    return asyncio.run(coro)


def test_empty_bank_returns_none():
    db = _FakeDB([])
    assert _run(get_photo_prompt(db)) is None
    assert _run(get_photo_prompt(db, tags=["nsfw"])) is None


def test_excludes_nsfw_unless_allow_nsfw():
    """NSFW is gated on the channel's age-restriction flag (``allow_nsfw``);
    requesting the 'nsfw' tag cannot re-enable it."""
    db = _FakeDB([
        ("photo", [], "Show us your desk right now."),
        ("photo", ["nsfw"], "Spicy challenge."),
        ("wyr", [], "Not a photo prompt."),
    ])
    # Default (no channel opt-in) → only the non-nsfw prompt.
    for _ in range(25):
        assert _run(get_photo_prompt(db)) == "Show us your desk right now."
    # Requesting the 'nsfw' tag without allow_nsfw → nsfw rows stay excluded,
    # and the remaining row doesn't carry the tag → filtered miss.
    for _ in range(25):
        assert _run(get_photo_prompt(db, tags=["nsfw"])) is None
    # Channel opt-in → the nsfw-tagged prompt qualifies under ANY-match.
    for _ in range(25):
        assert (
            _run(get_photo_prompt(db, tags=["nsfw"], allow_nsfw=True))
            == "Spicy challenge."
        )
    # Channel opt-in with no tag filter → both prompts are candidates.
    seen = {_run(get_photo_prompt(db, allow_nsfw=True)) for _ in range(40)}
    assert seen == {"Show us your desk right now.", "Spicy challenge."}


def test_tag_filter_any_match():
    db = _FakeDB([
        ("photo", ["food"], "Photo of your lunch."),
        ("photo", ["pets"], "Photo of your pet."),
        ("photo", [], "Untagged photo."),
    ])
    # food filter → only the food-tagged prompt.
    seen = {_run(get_photo_prompt(db, tags=["food"])) for _ in range(30)}
    assert seen == {"Photo of your lunch."}
    # food OR pets → both tagged prompts (untagged excluded).
    seen = {_run(get_photo_prompt(db, tags=["food", "pets"])) for _ in range(60)}
    assert seen == {"Photo of your lunch.", "Photo of your pet."}


def test_tag_filter_miss_returns_none():
    db = _FakeDB([("photo", ["food"], "Photo of your lunch.")])
    assert _run(get_photo_prompt(db, tags=["nope"])) is None


def test_ignores_other_game_types():
    db = _FakeDB([("wyr", [], "Not a photo prompt.")])
    assert _run(get_photo_prompt(db)) is None


def test_returns_one_of_several_candidates():
    prompts = {"A photo.", "B photo.", "C photo."}
    db = _FakeDB([("photo", [], p) for p in prompts])
    seen = {_run(get_photo_prompt(db)) for _ in range(50)}
    assert seen <= prompts and seen  # every pick is a real candidate


# ── Card counts (photo-external-102) ────────────────────────────────────────
#
# The daily card archives before anyone replies, so its history row can only
# be filled in afterwards: player_count = distinct members who posted an image
# in the 24 h after the card, round_count = images posted. Both come from the
# messages table's ingest-time media_kind — the same signal the photo_post
# faucet pays on — and never from message content.

PHOTO_CHAN = 7001
GUILD = 9001
BOT_ID = 4444
CARD_TS = 1_700_000_000  # epoch of the card's started_at


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _seed_card(db_path, *, game_id="card-1", started=CARD_TS, guild_id=0,
               player_count=0, channel_id=PHOTO_CHAN):
    from bot_modules.core.db_utils import open_db

    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO games_game_history (game_id, game_type, channel_id, host_id,"
            " player_count, round_count, payload, started_at, ended_at, guild_id)"
            " VALUES (?, 'photo', ?, 1, ?, 0, '{}', ?, ?, ?)",
            (game_id, channel_id, player_count, _iso(started), _iso(started), guild_id),
        )


def _seed_image(db_path, *, author_id, ts, channel_id=PHOTO_CHAN, media_kind="media"):
    from bot_modules.core.db_utils import open_db

    _seed_image.n = getattr(_seed_image, "n", 0) + 1
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts, media_kind)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_seed_image.n, GUILD, channel_id, author_id, ts, media_kind),
        )


def _card_row(db_path, game_id="card-1"):
    from bot_modules.core.db_utils import open_db

    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT player_count, round_count, guild_id FROM games_game_history WHERE game_id = ?",
            (game_id,),
        ).fetchone()


async def _backfill(db_path, *, now):
    return await backfill_card_counts(
        GamesDb(db_path), channel_id=PHOTO_CHAN, guild_id=GUILD,
        exclude_author_ids=[BOT_ID], now=now,
    )


async def test_backfill_counts_distinct_posters_and_photos_in_the_day_after(sync_db_path):
    _seed_card(sync_db_path)
    _seed_image(sync_db_path, author_id=11, ts=CARD_TS + 60)
    _seed_image(sync_db_path, author_id=11, ts=CARD_TS + 120)   # second photo, same member
    _seed_image(sync_db_path, author_id=22, ts=CARD_TS + 3600)
    _seed_image(sync_db_path, author_id=BOT_ID, ts=CARD_TS)      # the card itself
    _seed_image(sync_db_path, author_id=33, ts=CARD_TS - 10)     # before the card
    _seed_image(sync_db_path, author_id=44, ts=CARD_TS + BACKFILL_WINDOW_SECONDS)  # too late
    _seed_image(sync_db_path, author_id=55, ts=CARD_TS + 90, media_kind="gif")     # not a photo
    _seed_image(sync_db_path, author_id=66, ts=CARD_TS + 90, channel_id=1)         # elsewhere

    updated = await _backfill(sync_db_path, now=CARD_TS + BACKFILL_WINDOW_SECONDS + 5)

    assert updated == 1
    row = _card_row(sync_db_path)
    assert (row["player_count"], row["round_count"]) == (2, 3)


async def test_backfill_waits_until_the_window_has_closed(sync_db_path):
    # Filling in at hour 3 would freeze a number that is still climbing.
    _seed_card(sync_db_path)
    _seed_image(sync_db_path, author_id=11, ts=CARD_TS + 60)

    assert await _backfill(sync_db_path, now=CARD_TS + 3 * 3600) == 0
    assert _card_row(sync_db_path)["player_count"] == 0


async def test_backfill_repairs_the_legacy_guild_zero(sync_db_path):
    # Every card before migration 204 archived guild_id = 0 and so was
    # invisible to the guild-filtered stats page. The channel is the guild's
    # own photo channel, so the row can say so.
    _seed_card(sync_db_path, guild_id=0)
    await _backfill(sync_db_path, now=CARD_TS + BACKFILL_WINDOW_SECONDS + 5)
    assert _card_row(sync_db_path)["guild_id"] == GUILD


async def test_backfill_leaves_a_filled_row_alone(sync_db_path):
    _seed_card(sync_db_path, player_count=7)
    _seed_image(sync_db_path, author_id=11, ts=CARD_TS + 60)
    assert await _backfill(sync_db_path, now=CARD_TS + BACKFILL_WINDOW_SECONDS + 5) == 0
    assert _card_row(sync_db_path)["player_count"] == 7


async def test_backfill_records_a_card_nobody_answered_as_answered(sync_db_path):
    # A zero must mean "nobody posted", not "not counted yet" — so the row is
    # marked counted (guild fixed, counts written) even when the count is 0.
    _seed_card(sync_db_path, guild_id=0)
    assert await _backfill(sync_db_path, now=CARD_TS + BACKFILL_WINDOW_SECONDS + 5) == 1
    row = _card_row(sync_db_path)
    assert (row["player_count"], row["round_count"], row["guild_id"]) == (0, 0, GUILD)
    # …and it is not re-counted on the next launch.
    assert await _backfill(sync_db_path, now=CARD_TS + BACKFILL_WINDOW_SECONDS + 99) == 0


async def test_backfill_leaves_a_card_older_than_thirty_days_alone(sync_db_path):
    # The age floor compared strftime('%s') TEXT against an INTEGER, which in
    # SQLite is always true, so the 30-day floor never held.
    _seed_card(sync_db_path, started=CARD_TS - 31 * 86400)
    _seed_image(sync_db_path, author_id=11, ts=CARD_TS - 31 * 86400 + 60)
    assert await _backfill(sync_db_path, now=CARD_TS) == 0
    assert _card_row(sync_db_path)["player_count"] == 0


# ── Yesterday's recap (photo-external-105) ───────────────────────────────────
#
# Members posted into a stream and never heard back. The next card carries a
# one-line recap of the previous 24 h: photos, posters, and the most-loved
# photo (most reactions) as a jump link. Same ingest-time signals as the
# counts above — media_kind and the reaction tallies — no content is read.


def _seed_reactions(db_path, message_id, *counts):
    from bot_modules.core.db_utils import open_db

    with open_db(db_path) as conn:
        for i, n in enumerate(counts):
            conn.execute(
                "INSERT INTO message_reactions (message_id, emoji, count) VALUES (?, ?, ?)",
                (message_id, f"e{i}", n),
            )


async def test_recap_counts_the_day_and_picks_the_most_loved_photo(sync_db_path):
    from bot_modules.games_photo.logic import previous_day_recap

    now = CARD_TS + 86400
    _seed_image(sync_db_path, author_id=11, ts=now - 3600)
    first = _seed_image.n
    _seed_image(sync_db_path, author_id=11, ts=now - 3000)
    _seed_image(sync_db_path, author_id=22, ts=now - 2000)
    loved = _seed_image.n
    _seed_image(sync_db_path, author_id=BOT_ID, ts=now - 1000)          # the card itself
    _seed_image(sync_db_path, author_id=33, ts=now - 86400 - 5)         # yesterday's yesterday
    _seed_image(sync_db_path, author_id=44, ts=now - 500, media_kind="gif")
    _seed_reactions(sync_db_path, first, 2, 1)
    _seed_reactions(sync_db_path, loved, 4)

    recap = await previous_day_recap(
        GamesDb(sync_db_path), channel_id=PHOTO_CHAN, exclude_author_ids=[BOT_ID], now=now,
    )
    assert recap is not None
    assert (recap.photos, recap.posters) == (3, 2)
    assert recap.most_loved == (loved, 22, 4)


async def test_recap_is_none_when_nobody_posted(sync_db_path):
    from bot_modules.games_photo.logic import previous_day_recap

    assert await previous_day_recap(
        GamesDb(sync_db_path), channel_id=PHOTO_CHAN, exclude_author_ids=[BOT_ID], now=CARD_TS,
    ) is None


async def test_recap_has_no_most_loved_without_a_single_reaction(sync_db_path):
    from bot_modules.games_photo.logic import previous_day_recap

    now = CARD_TS + 86400
    _seed_image(sync_db_path, author_id=11, ts=now - 3600)
    recap = await previous_day_recap(
        GamesDb(sync_db_path), channel_id=PHOTO_CHAN, exclude_author_ids=[BOT_ID], now=now,
    )
    assert recap is not None
    assert (recap.photos, recap.posters, recap.most_loved) == (1, 1, None)


def test_recap_line_names_the_poster_and_links_the_photo():
    from bot_modules.games_photo.logic import DayRecap, recap_line

    recap = DayRecap(photos=17, posters=15, most_loved=(555, 22, 9))
    line = recap_line(recap, guild_id=1, channel_id=2, name_fn=lambda uid: f"U{uid}")
    assert line == (
        "Yesterday: 17 photos from 15 people — most loved: U22's, "
        "https://discord.com/channels/1/2/555"
    )
    assert "<@" not in line
    assert recap_line(DayRecap(1, 1, None), guild_id=1, channel_id=2, name_fn=str) == (
        "Yesterday: 1 photo from 1 person"
    )
