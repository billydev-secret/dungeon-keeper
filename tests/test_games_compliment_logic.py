"""Tests for the extracted Spin-the-Compliment pure-logic modules.

Covers ``bot_modules/games_compliment/logic.py`` (participant toggle,
pairing generation wrapper, payload serialisation) and
``bot_modules/games_compliment/embeds.py`` (lobby and pairings embeds,
line formatter). Mirrors the games_traditional template: the cog stays
thin; this module proves the extracted pieces work without spinning
up Discord.
"""

from __future__ import annotations

import random
import re

import pytest

from bot_modules.games_compliment.embeds import (
    build_lobby_embed,
    build_pairings_embed,
    format_pairing_line,
)
from bot_modules.games_compliment.logic import (
    generate_pairings,
    pairing_ids,
    serialize_pairings,
    join_participant,
    leave_participant,
)


# ── join_participant / leave_participant (social-prompt-46) ──────────


def test_join_adds_a_new_user_and_reports_it():
    payload: dict = {"participants": []}
    assert join_participant(payload, 42) is True
    assert payload["participants"] == [42]


def test_join_twice_is_refused_not_toggled():
    """The old single button toggled: a double-tap silently left the pool."""
    payload: dict = {"participants": [42]}
    assert join_participant(payload, 42) is False
    assert payload["participants"] == [42]


def test_leave_removes_only_that_user():
    payload: dict = {"participants": [1, 42, 3]}
    assert leave_participant(payload, 42) is True
    assert payload["participants"] == [1, 3]


def test_leave_when_not_in_pool_is_a_noop():
    payload: dict = {"participants": [1]}
    assert leave_participant(payload, 42) is False
    assert payload["participants"] == [1]


def test_join_and_leave_create_the_list_when_missing():
    payload: dict = {}
    assert leave_participant(payload, 1) is False
    assert join_participant(payload, 1) is True
    assert payload["participants"] == [1]


# ── generate_pairings ────────────────────────────────────────────────


def test_generate_pairings_empty_for_fewer_than_two():
    """One-player game can't be deranged — the shared helper returns {}."""
    assert generate_pairings([]) == {}
    assert generate_pairings([1]) == {}


def test_generate_pairings_every_player_is_giver_and_receiver():
    """Each id appears exactly once on each side of the mapping."""
    participants = [1, 2, 3, 4, 5]
    pairings = generate_pairings(participants)
    assert set(pairings.keys()) == set(participants)
    assert set(pairings.values()) == set(participants)


def test_generate_pairings_no_self_pairing():
    """Sattolo guarantees ``giver != receiver`` for every entry."""
    participants = list(range(2, 12))
    # Run multiple times because shuffle is random
    for _ in range(20):
        pairings = generate_pairings(participants)
        for giver, receiver in pairings.items():
            assert giver != receiver


def test_generate_pairings_two_player_swap():
    """Two players can only swap — every call returns ``{a: b, b: a}``."""
    pairings = generate_pairings([1, 2])
    assert pairings == {1: 2, 2: 1} or pairings == {2: 1, 1: 2}


# ── generate_pairings: the no-contact gate ───────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    [pytest.param({(1, 2)}, id="stored-order"), pytest.param({(2, 1)}, id="reversed")],
)
def test_generate_pairings_never_pairs_a_no_contact_pair_in_either_direction(forbidden):
    """A blocked pair is never giver->receiver *or* receiver->giver."""
    for _ in range(40):
        pairings = generate_pairings([1, 2, 3, 4, 5], forbidden)
        assert pairings[1] != 2 and pairings[2] != 1
        assert set(pairings) == {1, 2, 3, 4, 5}
        assert sorted(pairings.values()) == [1, 2, 3, 4, 5]


def test_generate_pairings_returns_empty_when_the_pool_cannot_be_paired():
    """Two players who are a no-contact pair: {} — the cog then refuses with
    its ordinary "need at least 2 players" copy, not a new message."""
    assert generate_pairings([1, 2], {(1, 2)}) == {}


# ── serialize_pairings ───────────────────────────────────────────────


def test_serialize_pairings_keys_become_strings():
    assert serialize_pairings({1: 2, 3: 4}) == {"1": 2, "3": 4}


def test_serialize_pairings_empty():
    assert serialize_pairings({}) == {}


def test_serialize_pairings_preserves_receiver_values():
    """Only the giver key is stringified; the receiver int is untouched."""
    result = serialize_pairings({100: 200})
    assert result["100"] == 200
    assert isinstance(result["100"], int)


# ── pairing_ids ──────────────────────────────────────────────────────


def test_pairing_ids_returns_each_user_once():
    pairings = {1: 2, 2: 3, 3: 1}
    ids = pairing_ids(pairings)
    assert sorted(ids) == [1, 2, 3]


def test_pairing_ids_dedupes_when_user_is_both_giver_and_receiver():
    """A circular triangle has every user listed as both giver and
    receiver — they should each appear once."""
    pairings = {1: 2, 2: 3, 3: 1}
    ids = pairing_ids(pairings)
    # exactly 3 ids, no duplicates
    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_pairing_ids_empty():
    assert pairing_ids({}) == []


def test_pairing_ids_preserves_iteration_order():
    """Ordering follows giver-then-receiver iteration of the dict."""
    pairings = {10: 20, 20: 30, 30: 10}
    ids = pairing_ids(pairings)
    # First giver is 10, its receiver is 20; then 30 from the third entry.
    assert ids[0] == 10
    assert 20 in ids
    assert 30 in ids


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_empty_pool_shows_dash():
    embed = build_lobby_embed("Alice", [])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"
    assert by_name["Pool (0)"] == "—"


def test_build_lobby_embed_lists_participants():
    embed = build_lobby_embed("Alice", ["Bob", "Carol"])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Pool (2)"] == "Bob, Carol"


def test_build_lobby_embed_title_uses_compliment_label():
    embed = build_lobby_embed("Alice", [])
    assert embed.title is not None
    assert "Spin the Compliment" in embed.title


def test_build_lobby_embed_has_footer():
    embed = build_lobby_embed("Alice", [])
    assert embed.footer.text is not None
    assert "Spin the Compliment" in embed.footer.text


# ── format_pairing_line ──────────────────────────────────────────────


def test_format_pairing_line_uses_arrow():
    assert format_pairing_line("<@1>", "<@2>") == "<@1> → <@2>"


def test_format_pairing_line_preserves_raw_strings():
    """Plain-string ids (no member resolved) still render with the arrow."""
    line = format_pairing_line("Alice", "Bob")
    assert "Alice" in line
    assert "Bob" in line
    assert "→" in line


# ── build_pairings_embed ─────────────────────────────────────────────

_MENTION = re.compile(r"<@!?\d+>")


def _named(uid: int) -> str:
    return f"Member{uid}"


def _seen(embed) -> str:
    parts = [embed.title or "", embed.description or "", embed.footer.text or ""]
    for f in embed.fields:
        parts += [f.name or "", f.value or ""]
    return "\n".join(parts)


def test_build_pairings_embed_title_and_color():
    embed = build_pairings_embed({1: 2, 2: 1})
    assert embed.title is not None
    assert "Compliment Pairings" in embed.title


def test_build_pairings_embed_lists_every_pairing_as_a_line():
    embed = build_pairings_embed({1: 2, 2: 3, 3: 1}, name_fn=_named)
    assert embed.description is not None
    for line in ("Member1 → Member2", "Member2 → Member3", "Member3 → Member1"):
        assert line in embed.description


def test_build_pairings_embed_names_members_never_mentions_them():
    """An embed mention is resolved by the *reading* client from its own
    cache, so it degrades to a bare number for anyone who hasn't seen the
    member. The pairings card is the only record once the ping is gone."""
    embed = build_pairings_embed({1: 2, 2: 1}, name_fn=_named)
    assert not _MENTION.search(_seen(embed))
    assert "Member1" in _seen(embed) and "Member2" in _seen(embed)


def test_build_pairings_embed_defaults_to_a_mention_for_an_unwired_caller():
    text = _seen(build_pairings_embed({1: 2, 2: 1}))
    assert "<@1> → <@2>" in text


def test_build_pairings_embed_appends_call_to_action():
    embed = build_pairings_embed({1: 2, 2: 1})
    assert embed.description is not None
    assert "your compliment" in embed.description.lower()


def test_build_pairings_embed_has_footer():
    embed = build_pairings_embed({1: 2, 2: 1})
    assert embed.footer.text is not None
    assert "Spin the Compliment" in embed.footer.text


# ── integration ──────────────────────────────────────────────────────


def test_full_lobby_flow_pair_of_users():
    """Two players join the pool, then pairings are generated."""
    payload: dict = {}
    join_participant(payload, 1)
    join_participant(payload, 2)
    assert payload["participants"] == [1, 2]
    pairings = generate_pairings(payload["participants"])
    # Two-player game must always swap
    assert pairings[1] == 2 and pairings[2] == 1


def test_serialize_then_pairing_ids_consistent():
    """Whatever ids appear in the pairings show up in pairing_ids."""
    random.seed(0)
    pairings = generate_pairings([10, 20, 30, 40])
    ids = pairing_ids(pairings)
    serialized = serialize_pairings(pairings)
    # Each serialized key (as int) is in ids
    for key in serialized:
        assert int(key) in ids


@pytest.mark.parametrize("pool_size", [2, 3, 4, 5, 8])
def test_generate_pairings_size_matches_pool(pool_size):
    pairings = generate_pairings(list(range(pool_size)))
    assert len(pairings) == pool_size


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_compliment_cog as compliment_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402
from bot_modules.services.no_contact_service import add_pair  # noqa: E402
from tests.fakes import FakeGuild, FakeMember, FakeUser, fake_interaction  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_close_generate_pays_nobody_until_the_wrap(monkeypatch, sync_db_path):
    """The pool is paid at the wrap-up (finish_wrap), not at Close & Generate
    (social-prompt-39): before this the round ended and paid before a single
    compliment was given."""
    spy = AsyncMock()
    monkeypatch.setattr(compliment_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    cog = compliment_cog.ComplimentCog(bot)  # type: ignore[arg-type]
    armed: list = []
    monkeypatch.setattr(cog, "arm_wrap", lambda channel, game_id: armed.append(game_id))
    gid = await create_game(bot.games_db, 100, 1, "compliment", payload={"participants": [1, 2, 3]})
    view = compliment_cog.ComplimentView(gid, 1, bot.games_db, bot, cog)  # type: ignore[arg-type]
    interaction = fake_interaction(user=FakeUser(id=1))
    interaction.guild = None
    interaction.followup.send = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock(), id=5))
    await view.close_generate.callback(interaction)
    spy.assert_not_awaited()
    assert armed == [gid]


def test_lobby_embed_renders_the_start_countdown():
    # start_in advertises a start time as a live Discord relative timestamp;
    # the host still presses the button.
    embed = build_lobby_embed("Alice", [], start_at=1_700_000_000)
    field = next(f for f in embed.fields if f.name == "⏰ Starting")
    assert field.value == "<t:1700000000:R>"


def test_lobby_embed_omits_the_countdown_when_none_was_set():
    embed = build_lobby_embed("Alice", [])
    assert all(f.name != "⏰ Starting" for f in embed.fields)


# ── Close & Generate: the no-contact gate and the resolved names ─────


def _guild_of(*uids: int) -> FakeGuild:
    guild = FakeGuild(id=9001)
    for uid in uids:
        guild.members[uid] = FakeMember(id=uid, name=f"user{uid}", display_name=f"Member{uid}")
    return guild


async def _close(monkeypatch, sync_db_path, participants: list[int]):
    """Press Close & Generate; returns the interaction and the stored payload
    (``pairings`` is present once the wrap opened, absent on a refusal)."""
    monkeypatch.setattr(compliment_cog, "audit_anonymous", AsyncMock())
    bot = _SpyBot(sync_db_path)
    cog = compliment_cog.ComplimentCog(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog, "arm_wrap", lambda channel, game_id: None)
    guild = _guild_of(*participants)
    gid = await create_game(
        bot.games_db, 100, participants[0], "compliment",
        payload={"participants": list(participants)},
    )
    view = compliment_cog.ComplimentView(gid, participants[0], bot.games_db, bot, cog)  # type: ignore[arg-type]
    interaction = fake_interaction(user=guild.members[participants[0]], guild=guild)
    interaction.followup.send = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock(), id=5))
    await view.close_generate.callback(interaction)
    from bot_modules.games.utils.game_manager import get_game_payload as _payload
    return interaction, await _payload(bot.games_db, gid)


async def test_close_generate_never_pairs_a_no_contact_pair(monkeypatch, sync_db_path):
    add_pair(sync_db_path, 9001, 1, 2, created_by=1, protected_user_id=1)
    for _ in range(10):
        interaction, payload = await _close(monkeypatch, sync_db_path, [1, 2, 3, 4])
        pairings = payload["pairings"]
        assert pairings["1"] != 2 and pairings["2"] != 1
        assert set(pairings) == {"1", "2", "3", "4"}


@pytest.mark.parametrize("pool", [[1, 2], [1, 2, 3]], ids=["two", "three"])
async def test_close_generate_refuses_an_unpairable_pool_with_the_ordinary_copy(monkeypatch, sync_db_path, pool):
    """A pool the no-contact pair leaves unpairable (two players, or three —
    every derangement of three joins every pair in one direction) gets the
    same 'need at least 2 players' reply a one-player pool gets."""
    add_pair(sync_db_path, 9001, 1, 2, created_by=1, protected_user_id=1)
    interaction, payload = await _close(monkeypatch, sync_db_path, pool)
    interaction.response.send_message.assert_awaited_once_with(
        "Need at least 2 players in the pool!", ephemeral=True
    )
    assert "pairings" not in payload
    interaction.followup.send.assert_not_awaited()


async def test_close_generate_embed_names_members_and_pings_in_content(monkeypatch, sync_db_path):
    interaction, _ = await _close(monkeypatch, sync_db_path, [1, 2, 3])
    calls = interaction.followup.send.await_args_list
    ping = next(c for c in calls if c.kwargs.get("content"))
    card = next(c for c in calls if c.kwargs.get("embed") is not None)
    assert "<@1>" in ping.kwargs["content"]
    text = _seen(card.kwargs["embed"])
    assert not _MENTION.search(text)
    assert "Member1" in text and "Member2" in text and "Member3" in text


# ── the wrap-up (social-prompt-39) ───────────────────────────────────

import asyncio  # noqa: E402
import time as _time  # noqa: E402

from bot_modules.cogs.games_config_cog import RECAP_ENDING_COGS  # noqa: E402
from bot_modules.core.db_utils import open_db  # noqa: E402
from bot_modules.games.utils.game_manager import (  # noqa: E402
    get_active_game_by_id,
    get_game_payload,
    modify_payload,
)
from bot_modules.games_compliment.embeds import build_wrap_recap_embed  # noqa: E402
from bot_modules.games_compliment.logic import (  # noqa: E402
    STATE_WRAPPING,
    WRAP_NUDGE_AFTER_SECONDS,
    WRAP_SECONDS,
    claim_wrap,
    delivered_givers,
    delivered_line,
    parse_pairings,
    release_wrap_claim,
    stragglers,
    wrap_schedule,
    wrap_seconds_remaining,
)
from bot_modules.services.message_store import store_message  # noqa: E402

GUILD = 4242
CH = 700


def _store(conn, mid: int, author: int, *, reply_to: int | None = None, mentions=(), ts: int = 1_000, channel: int = CH):
    store_message(
        conn, message_id=mid, guild_id=GUILD, channel_id=channel, author_id=author,
        content=None, reply_to_id=reply_to, ts=ts, attachment_urls=[], mention_ids=list(mentions),
        embeds=[], retain_content=False,
    )


PAIRINGS = {1: 2, 2: 3, 3: 1}


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        pytest.param(lambda c: None, set(), id="nothing-said"),
        pytest.param(lambda c: (_store(c, 10, 2, ts=990), _store(c, 11, 1, reply_to=10)), {1}, id="reply-to-receiver"),
        pytest.param(lambda c: _store(c, 11, 1, mentions=[2]), {1}, id="mention-of-receiver"),
        pytest.param(lambda c: _store(c, 11, 1, mentions=[3]), set(), id="mention-of-someone-else"),
        pytest.param(lambda c: (_store(c, 10, 3, ts=990), _store(c, 11, 1, reply_to=10)), set(), id="reply-to-someone-else"),
        pytest.param(lambda c: _store(c, 11, 1, mentions=[2], ts=900), set(), id="before-the-pairings"),
        pytest.param(lambda c: _store(c, 11, 1, mentions=[2], channel=CH + 1), set(), id="another-channel"),
        pytest.param(lambda c: _store(c, 11, 1), set(), id="bare-message-does-not-count"),
        pytest.param(
            lambda c: (_store(c, 11, 1, mentions=[2]), _store(c, 12, 2, mentions=[3]), _store(c, 13, 3, mentions=[1])),
            {1, 2, 3}, id="everyone",
        ),
    ],
)
def test_delivered_givers_reads_reply_and_mention_metadata(sync_db_path, setup, expected):
    """Content storage is off by default; reply and mention edges are still
    recorded, and that is all the wrap needs."""
    with open_db(sync_db_path) as conn:
        setup(conn)
        conn.commit()
        assert delivered_givers(conn, guild_id=GUILD, channel_id=CH, since_ts=1_000, pairings=PAIRINGS) == expected


def test_delivered_givers_with_no_pairings_is_empty(sync_db_path):
    with open_db(sync_db_path) as conn:
        assert delivered_givers(conn, guild_id=GUILD, channel_id=CH, since_ts=0, pairings={}) == set()


def test_parse_pairings_round_trips_and_tolerates_garbage():
    assert parse_pairings(serialize_pairings(PAIRINGS)) == PAIRINGS
    assert parse_pairings(None) == {}
    assert parse_pairings({"x": 1, "2": "y", "3": 4}) == {3: 4}


def test_stragglers_keeps_pairing_order():
    assert stragglers(PAIRINGS, {2}) == [1, 3]
    assert stragglers(PAIRINGS, {1, 2, 3}) == []


def test_wrap_schedule_is_five_then_ten_minutes():
    assert (WRAP_NUDGE_AFTER_SECONDS, WRAP_SECONDS) == (300, 600)
    assert wrap_schedule(1_000) == (1_300, 1_600)


@pytest.mark.parametrize(
    ("target", "now", "expected"),
    [
        pytest.param(1_600, 1_000, 600, id="fresh"),
        pytest.param(1_600, 1_598, 5, id="floor-when-nearly-due"),
        pytest.param(1_600, 9_000, 5, id="overdue-fires-promptly"),
        pytest.param(None, 1_000, 5, id="missing-target"),
    ],
)
def test_wrap_seconds_remaining(target, now, expected):
    assert wrap_seconds_remaining(target, now) == expected


@pytest.mark.parametrize(
    ("done", "total", "expected"),
    [
        pytest.param(0, 0, "No pairings this round.", id="none"),
        pytest.param(5, 6, "💛 **5** of **6** compliments delivered.", id="partial"),
        pytest.param(6, 6, "💛 All **6** compliments delivered!", id="all"),
    ],
)
def test_delivered_line(done, total, expected):
    assert delivered_line(done, total) == expected


def test_pairings_embed_marks_delivered_givers_and_shows_the_wrap_countdown():
    embed = build_pairings_embed(PAIRINGS, name_fn=str, delivered={2}, ends_at=1_600)
    assert embed.description is not None
    lines = embed.description.splitlines()
    assert lines[0] == "1 → 2"
    assert lines[1] == "✅ 2 → 3"
    assert "<t:1600:R>" in embed.description
    assert "@mention" in embed.description


def test_wrap_recap_embed_has_the_count_and_a_footer():
    embed = build_wrap_recap_embed(5, 6)
    assert embed.title is not None and "Wrap-Up" in embed.title
    assert embed.description == "💛 **5** of **6** compliments delivered."
    assert embed.footer.text is not None and "Spin the Compliment" in embed.footer.text


def test_claim_wrap_is_won_once_and_released_for_a_wrap_that_never_finished():
    """The ten-minute timer and the host's /games end both call finish_wrap;
    only the first claim may post the card and pay the pool."""
    payload: dict = {}
    assert claim_wrap(payload) is True
    assert claim_wrap(payload) is False
    # A live row at boot means the winner died before end_game paid, so the
    # claim is stale and recovery drops it.
    assert release_wrap_claim(payload) is True
    assert release_wrap_claim(payload) is False
    assert claim_wrap(payload) is True


def test_games_end_hands_a_wrapping_compliment_to_its_recap():
    assert RECAP_ENDING_COGS["compliment"] == "ComplimentCog"


# ── the cog's wrap beats, driven without the clock ───────────────────


class _Bot:
    def __init__(self, db_path):
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)
        self.added: list = []

    def get_guild(self, _gid):
        return None

    def add_view(self, view, *, message_id=None):
        self.added.append((view, message_id))


def _channel():
    guild = SimpleNamespace(id=GUILD, get_member=lambda _uid: None)
    return SimpleNamespace(id=CH, name="games", guild=guild, send=AsyncMock())


@pytest.fixture
def quiet(monkeypatch):
    async def _footer(*_a, **_kw):
        return None

    from bot_modules.economy import game_rewards

    monkeypatch.setattr(game_rewards, "append_payout_footer", _footer)


async def _wrapping_game(db_path, *, delivered_by=()):
    bot = _Bot(db_path)
    cog = compliment_cog.ComplimentCog(bot)  # type: ignore[arg-type]
    now = int(_time.time())
    gid = await create_game(bot.games_db, CH, 1, "compliment", state=STATE_WRAPPING, guild_id=GUILD, payload={
        "participants": [1, 2, 3], "pairings": serialize_pairings(PAIRINGS),
        "generated_at": now - 100, "wrap_nudge_at": now + 200, "wrap_ends_at": now + 500,
    })
    with open_db(db_path) as conn:
        for i, giver in enumerate(delivered_by):
            _store(conn, 500 + i, giver, mentions=[PAIRINGS[giver]], ts=now - 50)
        conn.commit()
    return bot, cog, gid


async def test_finish_wrap_posts_the_recap_pays_the_pool_and_records_who_delivered(sync_db_path, quiet, monkeypatch):
    bot, cog, gid = await _wrapping_game(sync_db_path, delivered_by=(1, 3))
    spy = AsyncMock()
    monkeypatch.setattr(compliment_cog, "end_game", spy)
    channel = _channel()

    assert await cog.finish_wrap(channel, gid) is True

    embed = channel.send.await_args.kwargs["embed"]
    assert "**2** of **3**" in embed.description
    assert isinstance(channel.send.await_args.kwargs["view"], compliment_cog.ComplimentRecapView)
    call = spy.await_args
    assert call is not None and call.kwargs["player_ids"] == [1, 2, 3]
    assert call.kwargs["bot"] is bot
    assert call.kwargs["payload"]["delivered"] == [1, 3]
    # A second finish (the timer firing after /games end got there) is a no-op.
    await bot.games_db.execute("DELETE FROM games_active_games WHERE game_id = ?", (gid,))
    assert await cog.finish_wrap(channel, gid) is False
    assert spy.await_count == 1


async def test_two_finish_wraps_at_once_post_and_pay_once(sync_db_path, quiet, monkeypatch):
    """The timer firing while the host runs /games end: both see a live row,
    so only the payload claim can stop the pool being paid twice."""
    bot, cog, gid = await _wrapping_game(sync_db_path, delivered_by=(1,))
    spy = AsyncMock()
    monkeypatch.setattr(compliment_cog, "end_game", spy)
    channel = _channel()

    results = await asyncio.gather(
        cog.finish_wrap(channel, gid), cog.finish_wrap(channel, gid),
    )

    assert sorted(results) == [False, True]
    assert spy.await_count == 1
    assert channel.send.await_count == 1


async def test_recovering_a_wrap_clears_a_stale_claim_so_it_can_finish(sync_db_path, quiet, monkeypatch):
    """A restart mid-finish leaves the row live with the claim set; the
    re-armed wrap must be able to win it, not refuse itself forever."""
    bot, cog, gid = await _wrapping_game(sync_db_path)
    def _stale_claim(payload: dict) -> None:
        claim_wrap(payload)

    await modify_payload(bot.games_db, gid, _stale_claim)
    monkeypatch.setattr(cog, "arm_wrap", lambda *_a: None)
    row = await get_active_game_by_id(bot.games_db, gid)
    payload = await get_game_payload(bot.games_db, gid)

    assert await cog.recover_game(row, payload, _channel(), SimpleNamespace(id=77)) is True

    assert "wrap_finished" not in await get_game_payload(bot.games_db, gid)
    spy = AsyncMock()
    monkeypatch.setattr(compliment_cog, "end_game", spy)
    assert await cog.finish_wrap(_channel(), gid) is True


async def test_wrap_nudge_reposts_with_checks_and_pings_only_the_stragglers(sync_db_path, quiet):
    bot, cog, gid = await _wrapping_game(sync_db_path, delivered_by=(2,))
    channel = _channel()

    assert await cog._wrap_nudge(channel, gid) is True

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"].startswith("<@1> <@3> ")
    assert "<@2>" not in kwargs["content"]
    assert kwargs["allowed_mentions"].users is True and kwargs["allowed_mentions"].roles is False
    lines = kwargs["embed"].description.splitlines()
    assert lines[1].startswith("✅ ") and not lines[0].startswith("✅")


async def test_wrap_nudge_is_silent_when_everyone_delivered(sync_db_path, quiet):
    bot, cog, gid = await _wrapping_game(sync_db_path, delivered_by=(1, 2, 3))
    channel = _channel()
    assert await cog._wrap_nudge(channel, gid) is True
    channel.send.assert_not_awaited()


async def test_wrap_nudge_stops_when_the_game_was_ended_elsewhere(sync_db_path, quiet):
    bot, cog, gid = await _wrapping_game(sync_db_path)
    await bot.games_db.execute("DELETE FROM games_active_games WHERE game_id = ?", (gid,))
    channel = _channel()
    assert await cog._wrap_nudge(channel, gid) is False
    channel.send.assert_not_awaited()


async def test_end_with_recap_finishes_a_wrap_but_not_a_lobby(sync_db_path, quiet, monkeypatch):
    bot, cog, gid = await _wrapping_game(sync_db_path)
    monkeypatch.setattr(compliment_cog, "end_game", AsyncMock())
    channel = _channel()
    assert await cog.end_with_recap(channel, gid) is True
    channel.send.assert_awaited_once()

    lobby = await create_game(bot.games_db, CH + 1, 1, "compliment", state="joining", payload={"participants": [1]})
    assert await cog.end_with_recap(_channel(), lobby) is False


async def test_recover_rearms_a_wrap_and_rebinds_a_lobby(sync_db_path, quiet, monkeypatch):
    bot, cog, gid = await _wrapping_game(sync_db_path)
    armed: list = []
    monkeypatch.setattr(cog, "arm_wrap", lambda channel, game_id: armed.append(game_id))
    row = await get_active_game_by_id(bot.games_db, gid)
    assert row is not None
    payload = await get_game_payload(bot.games_db, gid)
    assert await cog.recover_game(row, payload, _channel(), SimpleNamespace(id=1)) is True
    assert armed == [gid]

    lobby = await create_game(bot.games_db, CH + 1, 7, "compliment", state="joining", payload={"participants": [7]})
    row = await get_active_game_by_id(bot.games_db, lobby)
    assert row is not None
    assert await cog.recover_game(row, {"participants": [7]}, _channel(), SimpleNamespace(id=55)) is True
    view, mid = bot.added[-1]
    assert isinstance(view, compliment_cog.ComplimentView) and mid == 55
    assert view.host_id == 7 and bot.active_views[lobby] is view


async def test_close_and_generate_opens_the_wrap_instead_of_ending(sync_db_path, quiet, monkeypatch):
    """The pool is paid at the wrap-up, not at Close & Generate."""
    bot = _Bot(sync_db_path)
    cog = compliment_cog.ComplimentCog(bot)  # type: ignore[arg-type]
    gid = await create_game(bot.games_db, CH, 1, "compliment", state="joining", guild_id=GUILD, payload={"participants": [1, 2, 3]})
    view = compliment_cog.ComplimentView(gid, 1, bot.games_db, bot, cog)
    end_spy = AsyncMock()
    monkeypatch.setattr(compliment_cog, "end_game", end_spy)
    armed: list = []
    monkeypatch.setattr(cog, "arm_wrap", lambda channel, game_id: armed.append(game_id))

    async def _name_fn(**_kw):
        return str

    monkeypatch.setattr(compliment_cog, "build_name_fn", _name_fn)
    monkeypatch.setattr(compliment_cog, "no_contact_pairs_among", lambda *_a: set())
    monkeypatch.setattr(compliment_cog, "audit_anonymous", AsyncMock())
    sent = SimpleNamespace(id=9001, delete=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, display_name="Host", guild_permissions=SimpleNamespace(administrator=False, manage_guild=False, manage_messages=False)),
        guild=None, channel=_channel(),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=sent)),
        edit_original_response=AsyncMock(),
    )

    await view.close_generate.callback(interaction)  # type: ignore[arg-type]

    end_spy.assert_not_awaited()
    assert armed == [gid]
    row = await get_active_game_by_id(bot.games_db, gid)
    assert row is not None and row["state"] == STATE_WRAPPING
    payload = await get_game_payload(bot.games_db, gid)
    assert set(parse_pairings(payload["pairings"])) == {1, 2, 3}
    assert payload["wrap_ends_at"] - payload["generated_at"] == WRAP_SECONDS
    assert payload["pairings_message_id"] == 9001
    pairings_embed = interaction.followup.send.await_args_list[-1].kwargs["embed"]
    assert f"<t:{payload['wrap_ends_at']}:R>" in pairings_embed.description


async def test_spin_again_relaunches_under_the_presser_through_the_guard(sync_db_path, quiet, monkeypatch):
    bot = _Bot(sync_db_path)
    cog = compliment_cog.ComplimentCog(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new-gid")
    cog.launch = launch  # type: ignore[method-assign]
    monkeypatch.setattr(compliment_cog, "sign_off_game_chore", AsyncMock())
    await bot.games_db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)", (CH, GUILD),
    )
    from unittest.mock import MagicMock
    import discord

    def _member(uid, name, *, mod):
        m = MagicMock(spec=discord.Member)
        m.id, m.display_name = uid, name
        m.guild_permissions = SimpleNamespace(administrator=mod, manage_guild=mod)
        return m

    view = compliment_cog.ComplimentRecapView("old", 1, cog)
    inter = SimpleNamespace(
        user=_member(9, "Mod", mod=True),
        guild=SimpleNamespace(id=GUILD), guild_id=GUILD, channel_id=CH, channel=_channel(),
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )
    await view.spin_again.callback(inter)  # type: ignore[arg-type]
    assert launch.await_args.kwargs["host_id"] == 9

    inter.user = _member(5, "M", mod=False)
    await view.spin_again.callback(inter)  # type: ignore[arg-type]
    assert inter.response.send_message.await_args.args[0] == compliment_cog.SPIN_AGAIN_DENIED_TEXT
    assert launch.await_count == 1
