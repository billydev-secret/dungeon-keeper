"""Tests for the Pen Pals cog's DB helpers, matching logic, and flows.

Covers:

- Pure helpers — ``_channel_name`` slugging, ``_parse_tags`` tolerance,
  ``_cfg_allows_nsfw``.
- Bank drawing — tags-based NSFW gating, per-session no-repeat exclusion,
  the AI fallback chain in ``_draw_question``.
- Pool + session store — join/leave idempotence, FIFO ordering,
  session lifecycle (create / lookup / close / swaps).
- ``_do_pair`` — session + channel creation, NSFW channel flag, the
  duplicate-pairing guard (channel deleted, no second session).
- ``_handle_join`` — every ephemeral branch: unconfigured, role-gated,
  already-active, already-queued, and queue-up (joining only ever queues;
  pairing happens in ``_do_round``, never on join).
- ``_do_round`` — FIFO drain, odd-one-out, failed pairs counted as waiting,
  the last-month re-match cooldown, and the recent-partner no-repeat.

Discord objects are ``MagicMock(spec=...)`` so ``isinstance`` checks in the
cog pass without a gateway connection; the network-facing helpers
(``_create_channel``, ``_post_intro``, ``_refresh_panel``, ``generate_text``)
are monkeypatched at the module level.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs import pen_pals_cog as pp
from bot_modules.core.db_utils import open_db
from tests.fakes import FakeGuild, FakeRole, FakeUser, fake_interaction

GUILD_ID = 9001
_COOLDOWN = pp._MATCH_COOLDOWN_SECS


# ── Fixtures / builders ───────────────────────────────────────────────


def _configure(
    db_path,
    *,
    enabled: bool = True,
    category_id: int = 777,
    opt_in_role_id: int = 0,
    question_category: str = "sfw",
    guild_id: int = GUILD_ID,
) -> None:
    with open_db(db_path) as conn:
        pp._set_config(
            conn,
            guild_id,
            enabled=enabled,
            category_id=category_id,
            opt_in_role_id=opt_in_role_id,
            question_category=question_category,
            log_channel_id=0,
            auto_round_dow=-1,
            auto_round_hour=12,
            panel_channel_id=0,
        )


def _add_bank_question(db_path, text: str, tags: list[str] | None = None) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO games_question_bank (game_type, tags, question_text) VALUES (?, ?, ?)",
            (pp._GAME_TYPE, json.dumps(tags or []), text),
        )


def _pool_ids(db_path, guild_id: int = GUILD_ID) -> list[int]:
    with open_db(db_path) as conn:
        return [r["user_id"] for r in pp._get_pool(conn, guild_id)]


def _active_session(db_path, user_id: int, guild_id: int = GUILD_ID):
    with open_db(db_path) as conn:
        return pp._get_active_session(conn, guild_id, user_id)


def _make_guild_mock(*member_ids: int) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD_ID
    guild.name = "Test Guild"
    members = {
        uid: MagicMock(spec=discord.Member, id=uid, display_name=f"user{uid}", mention=f"<@{uid}>")
        for uid in member_ids
    }
    guild.get_member.side_effect = members.get
    category = MagicMock(spec=discord.CategoryChannel)
    guild.get_channel.return_value = category
    return guild


def _make_bot_mock(guild: MagicMock) -> MagicMock:
    bot = MagicMock(spec=discord.Client)
    bot.get_guild.return_value = guild
    return bot


@pytest.fixture
def pair_env(sync_db_path, monkeypatch):
    """A configured guild + monkeypatched Discord I/O for _do_pair tests.

    Returns (bot, channel, created) where *created* records the kwargs of
    every _create_channel call.
    """
    _configure(sync_db_path)
    guild = _make_guild_mock(1, 2, 3)
    bot = _make_bot_mock(guild)

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.mention = "#penpals"
    channel.delete = AsyncMock()
    created: list[dict] = []

    async def fake_create_channel(guild_, category_, user1_, user2_, *, nsfw=False):
        created.append({"nsfw": nsfw})
        return channel

    monkeypatch.setattr(pp, "_create_channel", fake_create_channel)
    monkeypatch.setattr(pp, "_post_intro", AsyncMock())
    monkeypatch.setattr(pp, "resolve_accent_color", AsyncMock(return_value=None))
    monkeypatch.setattr(pp, "generate_text", AsyncMock(return_value="AI question?"))
    return bot, channel, created


# ── _channel_name ─────────────────────────────────────────────────────


def test_channel_name_slugs_and_joins():
    assert pp._channel_name("Alice Smith", "Bob") == "penpals-alice-smith-bob"


def test_channel_name_truncates_long_names():
    name = pp._channel_name("a" * 50, "b" * 50)
    assert name.startswith("penpals-" + "a" * 20)
    assert len(name) <= 100


def test_channel_name_survives_symbol_only_names():
    # All-emoji display names slug to empty strings; the channel name
    # must still be non-empty and valid.
    name = pp._channel_name("🔥🔥🔥", "!!!")
    assert name.startswith("penpals")


# ── _parse_tags / _cfg_allows_nsfw ────────────────────────────────────


def test_parse_tags_handles_bad_data():
    assert pp._parse_tags('["nsfw", "deep"]') == {"nsfw", "deep"}
    assert pp._parse_tags(None) == set()
    assert pp._parse_tags("") == set()
    assert pp._parse_tags("not json") == set()


def test_cfg_allows_nsfw(sync_db_path):
    assert pp._cfg_allows_nsfw(None) is False
    _configure(sync_db_path, question_category="sfw")
    with open_db(sync_db_path) as conn:
        assert pp._cfg_allows_nsfw(pp._get_config(conn, GUILD_ID)) is False
    _configure(sync_db_path, question_category="all")
    with open_db(sync_db_path) as conn:
        assert pp._cfg_allows_nsfw(pp._get_config(conn, GUILD_ID)) is True


# ── _draw_from_bank ───────────────────────────────────────────────────


def test_draw_from_bank_empty_returns_none(sync_db_path):
    with open_db(sync_db_path) as conn:
        assert pp._draw_from_bank(conn, False, []) is None


def test_draw_from_bank_excludes_nsfw_by_default(sync_db_path):
    _add_bank_question(sync_db_path, "spicy?", ["nsfw"])
    _add_bank_question(sync_db_path, "mild?")
    with open_db(sync_db_path) as conn:
        for _ in range(20):
            assert pp._draw_from_bank(conn, False, []) == "mild?"


def test_draw_from_bank_includes_nsfw_when_allowed(sync_db_path):
    _add_bank_question(sync_db_path, "spicy?", ["nsfw"])
    with open_db(sync_db_path) as conn:
        assert pp._draw_from_bank(conn, True, []) == "spicy?"
        assert pp._draw_from_bank(conn, False, []) is None


def test_draw_from_bank_respects_exclusion(sync_db_path):
    _add_bank_question(sync_db_path, "q1")
    _add_bank_question(sync_db_path, "q2")
    with open_db(sync_db_path) as conn:
        assert pp._draw_from_bank(conn, False, ["q1"]) == "q2"
        assert pp._draw_from_bank(conn, False, ["q1", "q2"]) is None


def test_draw_from_bank_ignores_other_game_types(sync_db_path):
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO games_question_bank (game_type, tags, question_text) VALUES (?, ?, ?)",
            ("wyr", "[]", "wyr question"),
        )
    with open_db(sync_db_path) as conn:
        assert pp._draw_from_bank(conn, False, []) is None


# ── _draw_question fallback chain ─────────────────────────────────────


async def test_draw_question_prefers_bank(sync_db_path, monkeypatch):
    _add_bank_question(sync_db_path, "from the bank?")
    gen = AsyncMock(return_value="from the AI?")
    monkeypatch.setattr(pp, "generate_text", gen)
    q = await pp._draw_question(sync_db_path, "sess", False)
    assert q == "from the bank?"
    gen.assert_not_awaited()


async def test_draw_question_falls_back_to_ai(sync_db_path, monkeypatch):
    monkeypatch.setattr(pp, "generate_text", AsyncMock(return_value="from the AI?\nextra line"))
    q = await pp._draw_question(sync_db_path, "sess", False)
    assert q == "from the AI?"


async def test_draw_question_static_fallback_when_ai_fails(sync_db_path, monkeypatch):
    monkeypatch.setattr(pp, "generate_text", AsyncMock(return_value=None))
    q = await pp._draw_question(sync_db_path, "sess", False)
    assert q == pp._FALLBACK_QUESTION


async def test_draw_question_excludes_session_history(sync_db_path, monkeypatch):
    _add_bank_question(sync_db_path, "q1")
    monkeypatch.setattr(pp, "generate_text", AsyncMock(return_value=None))
    with open_db(sync_db_path) as conn:
        pp._record_question(conn, "sess", "q1")
    q = await pp._draw_question(sync_db_path, "sess", False)
    assert q == pp._FALLBACK_QUESTION  # bank exhausted for this session


# ── Pool helpers ──────────────────────────────────────────────────────


def test_pool_add_remove_idempotent(sync_db_path):
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1)
        pp._add_to_pool(conn, GUILD_ID, 1)  # duplicate join is a no-op
        assert pp._in_pool(conn, GUILD_ID, 1)
    assert _pool_ids(sync_db_path) == [1]
    with open_db(sync_db_path) as conn:
        pp._remove_from_pool(conn, GUILD_ID, 1)
        assert not pp._in_pool(conn, GUILD_ID, 1)


def test_pool_orders_by_joined_at(sync_db_path):
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 3, joined_at=300.0)
    assert _pool_ids(sync_db_path) == [1, 2, 3]


def test_add_to_pool_preserves_explicit_joined_at(sync_db_path):
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=123.0)
        row = pp._get_pool(conn, GUILD_ID)[0]
        assert row["joined_at"] == 123.0


# ── Session helpers ───────────────────────────────────────────────────


def test_session_lifecycle(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, now)
    with open_db(sync_db_path) as conn:
        for uid in (1, 2):
            s = pp._get_active_session(conn, GUILD_ID, uid)
            assert s is not None and s["session_id"] == "s1"
        assert pp._get_active_session(conn, GUILD_ID, 3) is None
        s = pp._get_session_by_channel(conn, 4242)
        assert s is not None and s["expiry_at"] == pytest.approx(now + pp._SESSION_SECS)
        assert s["next_question_at"] == pytest.approx(now + pp._Q_INTERVAL)
    with open_db(sync_db_path) as conn:
        pp._close_session(conn, "s1", "early")
    with open_db(sync_db_path) as conn:
        assert pp._get_active_session(conn, GUILD_ID, 1) is None
        assert pp._get_session_by_channel(conn, 4242) is None


def test_increment_swaps_counts_up(sync_db_path):
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, time.time())
    with open_db(sync_db_path) as conn:
        assert pp._increment_swaps(conn, "s1") == 1
        assert pp._increment_swaps(conn, "s1") == 2


def test_recent_partners_reads_both_sides(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "a", GUILD_ID, 1, 1, 2, now)
        pp._create_session(conn, "b", GUILD_ID, 2, 3, 1, now)
        assert pp._recent_partners(conn, GUILD_ID, 1) == {2, 3}
        assert pp._recent_partners(conn, GUILD_ID, 2) == {1}


# ── _do_pair ──────────────────────────────────────────────────────────


async def test_do_pair_creates_session_and_clears_pool(sync_db_path, pair_env):
    bot, channel, created = pair_env
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1)
        pp._add_to_pool(conn, GUILD_ID, 2)

    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is True

    session = _active_session(sync_db_path, 1)
    assert session is not None and session["channel_id"] == channel.id
    assert _pool_ids(sync_db_path) == []
    with open_db(sync_db_path) as conn:
        assert pp._get_shown_questions(conn, session["session_id"]) != []
    assert created == [{"nsfw": False}]


async def test_do_pair_nsfw_channel_when_category_all(sync_db_path, pair_env):
    bot, _channel, created = pair_env
    _configure(sync_db_path, question_category="all")
    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is True
    assert created == [{"nsfw": True}]


async def test_do_pair_guard_aborts_duplicate_session(sync_db_path, pair_env):
    bot, channel, _created = pair_env
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "existing", GUILD_ID, 999, 2, 3, time.time())

    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is False
    channel.delete.assert_awaited()          # orphan channel cleaned up
    assert _active_session(sync_db_path, 1) is None


async def test_do_pair_refuses_when_disabled(sync_db_path, pair_env):
    bot, _channel, created = pair_env
    _configure(sync_db_path, enabled=False)
    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is False
    assert created == []


async def test_do_pair_refuses_missing_member(sync_db_path, pair_env):
    bot, _channel, created = pair_env
    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 999) is False
    assert created == []


# ── _handle_join ──────────────────────────────────────────────────────


def _join_interaction(user_id: int = 1, *, roles: list | None = None, guild: FakeGuild | None = None):
    g = guild or FakeGuild(id=GUILD_ID)
    user = FakeUser(id=user_id, roles=roles or [])
    g.members[user_id] = user
    return fake_interaction(user=user, guild=g)


async def test_handle_join_unconfigured(sync_db_path):
    interaction = _join_interaction()
    await pp._handle_join(interaction, sync_db_path)
    msg = interaction.response.send_message.await_args.args[0]
    assert "isn't set up" in msg


async def test_handle_join_role_gate_blocks(sync_db_path):
    _configure(sync_db_path, opt_in_role_id=555)
    g = FakeGuild(id=GUILD_ID)
    g.roles[555] = FakeRole(id=555, name="Verified")
    interaction = _join_interaction(guild=g)
    await pp._handle_join(interaction, sync_db_path)
    msg = interaction.response.send_message.await_args.args[0]
    assert "Verified" in msg
    assert _pool_ids(sync_db_path) == []


async def test_handle_join_role_gate_passes_with_role(sync_db_path, monkeypatch):
    _configure(sync_db_path, opt_in_role_id=555)
    role = FakeRole(id=555, name="Verified")
    g = FakeGuild(id=GUILD_ID)
    g.roles[555] = role
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    interaction = _join_interaction(roles=[role], guild=g)
    await pp._handle_join(interaction, sync_db_path)
    assert _pool_ids(sync_db_path) == [1]


async def test_handle_join_queues_first_user(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    refresh = AsyncMock()
    monkeypatch.setattr(pp, "_refresh_panel", refresh)
    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)
    assert _pool_ids(sync_db_path) == [1]
    msg = interaction.response.send_message.await_args.args[0]
    assert "You're in the pool" in msg
    refresh.assert_awaited()


async def test_handle_join_blocks_double_queue(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1)
    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)
    msg = interaction.response.send_message.await_args.args[0]
    assert "already in the pool" in msg
    assert _pool_ids(sync_db_path) == [1]


async def test_handle_join_blocks_active_session(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 42, 1, 2, time.time())
    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)
    msg = interaction.response.send_message.await_args.args[0]
    assert "already have an active pen pal" in msg


async def test_handle_join_never_pairs_on_join(sync_db_path, monkeypatch):
    # Even with a partner already waiting, joining only queues — pairing is
    # the round's job now, so the cooldown can't be bypassed on join.
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    do_pair.assert_not_awaited()
    assert _pool_ids(sync_db_path) == [2, 1]  # both waiting, FIFO preserved
    msg = interaction.response.send_message.await_args.args[0]
    assert "in the pool" in msg


# ── _handle_leave ─────────────────────────────────────────────────────


async def test_handle_leave_removes_from_pool(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1)
    refresh = AsyncMock()
    monkeypatch.setattr(pp, "_refresh_panel", refresh)
    interaction = _join_interaction(1)
    await pp._handle_leave(interaction, sync_db_path)
    assert _pool_ids(sync_db_path) == []
    refresh.assert_awaited()


async def test_handle_leave_when_not_queued(sync_db_path):
    _configure(sync_db_path)
    interaction = _join_interaction(1)
    await pp._handle_leave(interaction, sync_db_path)
    msg = interaction.response.send_message.await_args.args[0]
    assert "not in the pool" in msg


# ── _do_round ─────────────────────────────────────────────────────────


async def test_do_round_pairs_fifo(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        for i, uid in enumerate([1, 2, 3, 4], start=1):
            pp._add_to_pool(conn, GUILD_ID, uid, joined_at=float(i))

    calls: list[tuple[int, int]] = []

    async def fake_pair(bot, db_path, guild_id, u1, u2):
        calls.append((u1, u2))
        with open_db(db_path) as conn:
            pp._remove_from_pool(conn, guild_id, u1)
            pp._remove_from_pool(conn, guild_id, u2)
        return True

    monkeypatch.setattr(pp, "_do_pair", fake_pair)
    pairs, waiting = await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)
    assert pairs == 2 and waiting == 0
    assert calls == [(1, 2), (3, 4)]


async def test_do_round_leaves_odd_one_out(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        for i, uid in enumerate([1, 2, 3], start=1):
            pp._add_to_pool(conn, GUILD_ID, uid, joined_at=float(i))

    async def fake_pair(bot, db_path, guild_id, u1, u2):
        with open_db(db_path) as conn:
            pp._remove_from_pool(conn, guild_id, u1)
            pp._remove_from_pool(conn, guild_id, u2)
        return True

    monkeypatch.setattr(pp, "_do_pair", fake_pair)
    pairs, waiting = await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)
    assert pairs == 1 and waiting == 1
    assert _pool_ids(sync_db_path) == [3]


async def test_do_round_counts_failed_pairs_as_waiting(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        for i, uid in enumerate([1, 2], start=1):
            pp._add_to_pool(conn, GUILD_ID, uid, joined_at=float(i))

    monkeypatch.setattr(pp, "_do_pair", AsyncMock(return_value=False))
    pairs, waiting = await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)
    assert pairs == 0 and waiting == 2
    assert _pool_ids(sync_db_path) == [1, 2]  # nobody silently dropped


async def test_do_round_avoids_recent_repeat_when_possible(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        # 1 and 2 were paired before — but long enough ago to clear the
        # month-long cooldown, so they're eligible and only the no-repeat
        # preference should steer 1 away from 2. 3 is fresh.
        old = time.time() - (_COOLDOWN + 86400)
        pp._create_session(conn, "old", GUILD_ID, 5, 1, 2, old)
        pp._close_session(conn, "old", "expired")
        for i, uid in enumerate([1, 2, 3], start=1):
            pp._add_to_pool(conn, GUILD_ID, uid, joined_at=float(i))

    calls: list[tuple[int, int]] = []

    async def fake_pair(bot, db_path, guild_id, u1, u2):
        calls.append((u1, u2))
        with open_db(db_path) as conn:
            pp._remove_from_pool(conn, guild_id, u1)
            pp._remove_from_pool(conn, guild_id, u2)
        return True

    monkeypatch.setattr(pp, "_do_pair", fake_pair)
    await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)
    assert calls == [(1, 3)]
    assert _pool_ids(sync_db_path) == [2]


async def test_do_round_skips_members_matched_within_the_month(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        # 1 was matched a week ago → still cooling down; 2 and 3 are fresh.
        pp._create_session(conn, "recent", GUILD_ID, 5, 1, 99, time.time() - 7 * 86400)
        pp._close_session(conn, "recent", "expired")
        for i, uid in enumerate([1, 2, 3], start=1):
            pp._add_to_pool(conn, GUILD_ID, uid, joined_at=float(i))

    calls: list[tuple[int, int]] = []

    async def fake_pair(bot, db_path, guild_id, u1, u2):
        calls.append((u1, u2))
        with open_db(db_path) as conn:
            pp._remove_from_pool(conn, guild_id, u1)
            pp._remove_from_pool(conn, guild_id, u2)
        return True

    monkeypatch.setattr(pp, "_do_pair", fake_pair)
    pairs, waiting = await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)
    # Only 2 & 3 are eligible; 1 stays untouched and counts as waiting.
    assert calls == [(2, 3)]
    assert pairs == 1 and waiting == 1
    assert _pool_ids(sync_db_path) == [1]


async def test_do_round_eligible_once_cooldown_elapses(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        # Both last matched just over a month ago → both eligible again.
        old = time.time() - (_COOLDOWN + 86400)
        pp._create_session(conn, "a", GUILD_ID, 5, 1, 98, old)
        pp._close_session(conn, "a", "expired")
        pp._create_session(conn, "b", GUILD_ID, 6, 2, 97, old)
        pp._close_session(conn, "b", "expired")
        for i, uid in enumerate([1, 2], start=1):
            pp._add_to_pool(conn, GUILD_ID, uid, joined_at=float(i))

    async def fake_pair(bot, db_path, guild_id, u1, u2):
        with open_db(db_path) as conn:
            pp._remove_from_pool(conn, guild_id, u1)
            pp._remove_from_pool(conn, guild_id, u2)
        return True

    monkeypatch.setattr(pp, "_do_pair", fake_pair)
    pairs, waiting = await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)
    assert pairs == 1 and waiting == 0
    assert _pool_ids(sync_db_path) == []


# ── Panel refresh serialization ───────────────────────────────────────


async def test_refresh_panel_serializes_per_guild(sync_db_path, monkeypatch):
    """Concurrent repost requests run one at a time (no duplicate panels)."""
    running = 0
    max_running = 0

    async def fake_locked(bot, db_path, guild_id, *, repost=False):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        running -= 1

    monkeypatch.setattr(pp, "_refresh_panel_locked", fake_locked)
    pp._panel_refresh_locks.clear()
    await asyncio.gather(
        *(pp._refresh_panel(MagicMock(), sync_db_path, GUILD_ID, repost=True) for _ in range(5))
    )
    assert max_running == 1


async def test_refresh_panel_noop_without_config(sync_db_path):
    # No pen_pals_config row at all: must return quietly without touching Discord.
    bot = MagicMock(spec=discord.Client)
    await pp._refresh_panel(bot, sync_db_path, GUILD_ID)
    bot.get_channel.assert_not_called()
