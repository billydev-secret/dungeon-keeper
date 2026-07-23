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
  already-active, already-queued, queue-up, and instant matching (joining
  pairs on the spot when someone eligible is waiting, and every reason it
  falls back to queuing: empty pool, either side on cooldown, a waiting
  member who is already in a chat, a failed pairing).
- ``_pick_partner`` / ``_eligible_pool`` — no-repeat preference, oldest-first
  fallback, and the one-chat-at-a-time exclusion.
- ``_do_round`` — FIFO drain of whoever is left over, odd-one-out, failed
  pairs counted as waiting, the re-match cooldown, and the no-repeat rule.

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


# ── _post_intro ───────────────────────────────────────────────────────


async def test_post_intro_embed_lists_new_question_and_end_commands():
    channel = MagicMock(spec=discord.TextChannel)
    intro_msg = MagicMock()
    intro_msg.pin = AsyncMock()
    channel.send = AsyncMock(side_effect=[intro_msg, MagicMock()])
    user1 = FakeUser(1, "Alice")
    user2 = FakeUser(2, "Bob")

    await pp._post_intro(channel, user1, user2, time.time() + 3600, "A question?")

    embed = channel.send.call_args_list[0].kwargs["embed"]
    commands_field = next(f for f in embed.fields if f.name == "Commands")
    assert "/penpals new-question" in commands_field.value
    assert "/penpals end" in commands_field.value


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


def test_draw_from_bank_is_round_robin_not_repeating_until_pool_cycles(sync_db_path):
    """The small pen_pals pool shouldn't repeat a question across separate
    sessions while an unserved row is still available — each draw marks the
    row served, so every row in the pool gets used once before any repeat."""
    _add_bank_question(sync_db_path, "q1")
    _add_bank_question(sync_db_path, "q2")
    _add_bank_question(sync_db_path, "q3")
    drawn = []
    with open_db(sync_db_path) as conn:
        for _ in range(3):
            drawn.append(pp._draw_from_bank(conn, False, []))
    assert sorted(drawn) == ["q1", "q2", "q3"]


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


def test_create_session_uses_configured_session_seconds_not_hardcoded_default(sync_db_path):
    """A custom session_seconds value drives expiry_at, not the module default."""
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, now, session_seconds=600)
    with open_db(sync_db_path) as conn:
        s = pp._get_session_by_channel(conn, 4242)
        assert s is not None and s["expiry_at"] == pytest.approx(now + 600)


def test_set_and_get_timers_round_trip(sync_db_path):
    with open_db(sync_db_path) as conn:
        cfg = pp._get_config(conn, GUILD_ID)
        assert cfg is None
    with open_db(sync_db_path) as conn:
        pp._set_timers(
            conn, GUILD_ID,
            session_seconds=1800, match_cooldown_seconds=86400,
            max_question_swaps=1, warn_seconds=300, question_suppress_seconds=600,
        )
    with open_db(sync_db_path) as conn:
        cfg = pp._get_config(conn, GUILD_ID)
        assert cfg["session_seconds"] == 1800
        assert cfg["match_cooldown_seconds"] == 86400
        assert cfg["max_question_swaps"] == 1
        assert cfg["warn_seconds"] == 300
        assert cfg["question_suppress_seconds"] == 600


def test_new_config_row_defaults_match_old_hardcoded_constants(sync_db_path):
    """A freshly _set_config'd guild (no explicit timer overrides) must default
    to the same values that used to be hardcoded module constants — no
    behavior change for existing guilds that never touch the new panel."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        cfg = pp._get_config(conn, GUILD_ID)
        assert cfg["session_seconds"] == pp._SESSION_SECS
        assert cfg["match_cooldown_seconds"] == pp._MATCH_COOLDOWN_SECS
        assert cfg["max_question_swaps"] == pp._MAX_SWAPS
        assert cfg["warn_seconds"] == pp._WARN_SECS
        assert cfg["question_suppress_seconds"] == pp._Q_SUPPRESS_SECS


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


async def test_do_pair_uses_configured_session_seconds_not_hardcoded_default(sync_db_path, pair_env):
    """A guild with a configured session length gets that expiry, not _SESSION_SECS."""
    bot, _channel, _created = pair_env
    with open_db(sync_db_path) as conn:
        pp._set_timers(
            conn, GUILD_ID,
            session_seconds=120, match_cooldown_seconds=pp._MATCH_COOLDOWN_SECS,
            max_question_swaps=pp._MAX_SWAPS, warn_seconds=pp._WARN_SECS,
            question_suppress_seconds=pp._Q_SUPPRESS_SECS,
        )

    before = time.time()
    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is True
    after = time.time()

    session = _active_session(sync_db_path, 1)
    assert session is not None
    assert before + 120 <= session["expiry_at"] <= after + 120
    assert session["expiry_at"] < before + pp._SESSION_SECS


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


def _set_cooldown(db_path, seconds: int, guild_id: int = GUILD_ID) -> None:
    with open_db(db_path) as conn:
        pp._set_timers(
            conn,
            guild_id,
            session_seconds=86400,
            match_cooldown_seconds=seconds,
            max_question_swaps=3,
            warn_seconds=3600,
            question_suppress_seconds=7200,
        )


async def test_handle_join_pairs_instantly_when_someone_is_waiting(sync_db_path, monkeypatch):
    """A match on the table is taken now, not held for the next round."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    assert do_pair.await_args.args[2:] == (GUILD_ID, 1, 2)
    interaction.response.defer.assert_awaited()
    assert "Matched" in interaction.followup.send.await_args.args[0]


async def test_handle_join_queues_when_nobody_is_waiting(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    do_pair.assert_not_awaited()
    assert _pool_ids(sync_db_path) == [1]
    assert "in the pool" in interaction.response.send_message.await_args.args[0]


async def test_handle_join_skips_waiting_member_on_cooldown(sync_db_path, monkeypatch):
    """The rest period still holds — instant matching can't bypass it."""
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 172800)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
        pp._create_session(conn, "recent", GUILD_ID, 99, 2, 3, time.time() - 3600)
        pp._close_session(conn, "recent", "expired")
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    await pp._handle_join(_join_interaction(1), sync_db_path)

    do_pair.assert_not_awaited()
    assert _pool_ids(sync_db_path) == [2, 1]


async def test_handle_join_skips_when_joiner_is_on_cooldown(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 172800)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
        pp._create_session(conn, "recent", GUILD_ID, 99, 1, 3, time.time() - 3600)
        pp._close_session(conn, "recent", "expired")
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    await pp._handle_join(_join_interaction(1), sync_db_path)

    do_pair.assert_not_awaited()
    assert _pool_ids(sync_db_path) == [2, 1]


async def test_handle_join_never_matches_someone_already_in_a_chat(sync_db_path, monkeypatch):
    """One chat at a time: a stale pool row must not hand out a second channel."""
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)  # stale row
        pp._create_session(conn, "busy", GUILD_ID, 99, 2, 3, time.time())
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    do_pair.assert_not_awaited()
    assert "in the pool" in interaction.response.send_message.await_args.args[0]
    assert _pool_ids(sync_db_path) == [2, 1]


async def test_handle_join_keeps_joiner_pooled_when_pairing_fails(sync_db_path, monkeypatch):
    """A failed pairing (perms, lost race) must not cost the joiner their spot."""
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
    monkeypatch.setattr(pp, "_do_pair", AsyncMock(return_value=False))
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    assert _pool_ids(sync_db_path) == [2, 1]
    assert "in the pool" in interaction.followup.send.await_args.args[0]


async def test_handle_join_prefers_a_partner_you_havent_had(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)  # past partner, first in
        pp._add_to_pool(conn, GUILD_ID, 3, joined_at=200.0)
        pp._create_session(conn, "old", GUILD_ID, 99, 1, 2, time.time() - 86400)
        pp._close_session(conn, "old", "expired")
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    await pp._handle_join(_join_interaction(1), sync_db_path)

    assert do_pair.await_args.args[4] == 3


# ── _pick_partner / _eligible_pool ────────────────────────────────────


def test_pick_partner_prefers_first_non_recent():
    assert pp._pick_partner([2, 3, 4], {2, 3}) == 4


def test_pick_partner_falls_back_to_oldest_when_all_recent():
    assert pp._pick_partner([2, 3], {2, 3}) == 2


def test_pick_partner_returns_none_for_empty_pool():
    assert pp._pick_partner([], set()) is None


def test_eligible_pool_excludes_members_already_in_a_session(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
        pp._create_session(conn, "busy", GUILD_ID, 99, 2, 3, time.time())
        assert pp._eligible_pool(conn, GUILD_ID, time.time(), 0) == [1]


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


async def test_do_round_uses_configured_match_cooldown_not_hardcoded_default(sync_db_path, monkeypatch):
    """A guild with a short configured cooldown re-matches a member who'd
    still be blocked under the hardcoded 30-day default."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._set_timers(
            conn, GUILD_ID,
            session_seconds=pp._SESSION_SECS, match_cooldown_seconds=3600,
            max_question_swaps=pp._MAX_SWAPS, warn_seconds=pp._WARN_SECS,
            question_suppress_seconds=pp._Q_SUPPRESS_SECS,
        )
        # 1 was matched a week ago — still inside the hardcoded 30-day
        # default, but well past the guild's configured 1-hour cooldown.
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
    # All 3 (including the recently-matched 1) are eligible under the short cooldown.
    assert pairs == 1 and waiting == 1
    assert 1 not in _pool_ids(sync_db_path)


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


# ── Block list / separations ──────────────────────────────────────────


def test_is_blocked_pair_is_symmetric_for_a_member_block(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_block(conn, GUILD_ID, 1, 2)  # 1 blocks 2
        # Symmetric: one side blocking is enough, in either lookup order.
        assert pp._is_blocked_pair(conn, GUILD_ID, 1, 2) is True
        assert pp._is_blocked_pair(conn, GUILD_ID, 2, 1) is True
        # Unrelated pair is untouched.
        assert pp._is_blocked_pair(conn, GUILD_ID, 1, 3) is False


def test_member_block_add_get_remove_roundtrip(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_block(conn, GUILD_ID, 1, 2)
        pp._add_block(conn, GUILD_ID, 1, 3)
        pp._add_block(conn, GUILD_ID, 1, 2)  # idempotent
        assert set(pp._get_member_blocks(conn, GUILD_ID, 1)) == {2, 3}
        pp._remove_block(conn, GUILD_ID, 1, 2)
        assert pp._get_member_blocks(conn, GUILD_ID, 1) == [3]


def test_admin_separations_normalize_and_dedupe(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        # Same couple entered both orders, plus a self-pair that must drop.
        pp._set_admin_separations(conn, GUILD_ID, [(2, 1), (1, 2), (3, 3), (4, 5)])
        seps = pp._get_admin_separations(conn, GUILD_ID)
        assert set(seps) == {(1, 2), (4, 5)}  # normalized (min, max), deduped
        assert pp._is_blocked_pair(conn, GUILD_ID, 1, 2) is True
        assert pp._is_blocked_pair(conn, GUILD_ID, 5, 4) is True


def test_set_admin_separations_leaves_member_blocks_alone(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_block(conn, GUILD_ID, 1, 9)          # member block
        pp._set_admin_separations(conn, GUILD_ID, [(1, 2)])
        # Replacing admin separations doesn't touch the member's own list…
        assert pp._get_member_blocks(conn, GUILD_ID, 1) == [9]
        # …and the member block isn't surfaced as an admin separation.
        assert pp._get_admin_separations(conn, GUILD_ID) == [(1, 2)]


def test_member_unblock_does_not_remove_admin_separation(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._set_admin_separations(conn, GUILD_ID, [(1, 2)])
        pp._remove_block(conn, GUILD_ID, 1, 2)  # member-source delete only
        assert pp._is_blocked_pair(conn, GUILD_ID, 1, 2) is True


def test_find_instant_match_excludes_a_blocked_candidate(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
        pp._add_block(conn, GUILD_ID, 1, 2)
        assert pp._find_instant_match(conn, GUILD_ID, 1) is None
        # A non-blocked waiter is still matched.
        pp._add_to_pool(conn, GUILD_ID, 3, joined_at=200.0)
        assert pp._find_instant_match(conn, GUILD_ID, 1) == 3


async def test_handle_join_queues_when_only_candidate_is_blocked(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
        pp._add_block(conn, GUILD_ID, 1, 2)
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    do_pair.assert_not_awaited()
    assert "in the pool" in interaction.response.send_message.await_args.args[0]
    assert _pool_ids(sync_db_path) == [2, 1]


async def test_do_round_pairs_around_a_blocked_pair(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        for i, uid in enumerate([1, 2, 3], start=1):
            pp._add_to_pool(conn, GUILD_ID, uid, joined_at=float(i))
        pp._add_block(conn, GUILD_ID, 1, 2)  # 1 won't take 2…

    calls: list[tuple[int, int]] = []

    async def fake_pair(bot, db_path, guild_id, u1, u2):
        calls.append((u1, u2))
        with open_db(db_path) as conn:
            pp._remove_from_pool(conn, guild_id, u1)
            pp._remove_from_pool(conn, guild_id, u2)
        return True

    monkeypatch.setattr(pp, "_do_pair", fake_pair)
    pairs, waiting = await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)
    # 1 pairs with 3 instead of the blocked 2; 2 is left waiting.
    assert calls == [(1, 3)]
    assert pairs == 1 and waiting == 1
    assert _pool_ids(sync_db_path) == [2]


async def test_do_round_leaves_both_pooled_when_the_only_pair_is_blocked(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=1.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=2.0)
        pp._add_block(conn, GUILD_ID, 2, 1)  # blocked either direction

    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    pairs, waiting = await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)

    do_pair.assert_not_awaited()
    assert pairs == 0 and waiting == 2
    assert _pool_ids(sync_db_path) == [1, 2]  # nobody forced, nobody dropped


async def test_do_pair_refuses_a_blocked_pair(sync_db_path, pair_env):
    """The safety net: even a direct pair (admin force, lost race) is refused."""
    bot, _channel, created = pair_env
    with open_db(sync_db_path) as conn:
        pp._add_block(conn, GUILD_ID, 1, 2)
    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is False
    assert created == []  # no channel ever created


def test_block_panel_content_empty_and_populated():
    guild = _make_guild_mock(2)
    assert "no blocks" in pp._block_panel_content(guild, []).lower()
    body = pp._block_panel_content(guild, [2])
    assert "user2" in body  # resolved display name
    body_left = pp._block_panel_content(guild, [999])
    assert "User 999" in body_left  # left-server fallback


async def test_handle_block_renders_current_blocklist(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_block(conn, GUILD_ID, 1, 2)
    g = FakeGuild(id=GUILD_ID)
    g.members[2] = FakeUser(id=2, display_name="Blocked Person")
    interaction = _join_interaction(1, guild=g)
    await pp._handle_block(interaction, sync_db_path)
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["view"], pp._PenPalsBlockView)
    assert "Blocked Person" in interaction.response.send_message.await_args.args[0]


async def test_penpals_pair_command_refuses_a_blocked_pair(sync_db_path):
    """The admin force-pair gives a clear reason instead of silently failing."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_block(conn, GUILD_ID, 10, 20)

    ctx = MagicMock(db_path=sync_db_path)
    cog = pp.PenPalsCog(MagicMock(), ctx)
    g = FakeGuild(id=GUILD_ID)
    u1 = FakeUser(id=10, display_name="Ten")
    u2 = FakeUser(id=20, display_name="Twenty")
    interaction = fake_interaction(user=FakeUser(id=1), guild=g)

    await cog.penpals_pair.callback(cog, interaction, u1, u2)

    msg = interaction.response.send_message.await_args.args[0]
    assert "blocked" in msg.lower()


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


# ── _tick pool sweep ──────────────────────────────────────────────────


async def test_tick_sweeps_pool_when_two_members_are_eligible(sync_db_path, monkeypatch):
    """Backlogs clear on their own — no scheduled round to wait for.

    Instant matching only fires on join, so two members who were ineligible
    then (cooldown, mid-session) would otherwise sit there indefinitely.
    """
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
    do_round = AsyncMock(return_value=(1, 0))
    monkeypatch.setattr(pp, "_do_round", do_round)

    await pp._tick(MagicMock(), sync_db_path)

    assert do_round.await_args.args[2] == GUILD_ID


async def test_tick_skips_sweep_without_two_eligible_members(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
        pp._create_session(conn, "busy", GUILD_ID, 99, 2, 3, time.time())  # 2 is chatting
    do_round = AsyncMock(return_value=(0, 1))
    monkeypatch.setattr(pp, "_do_round", do_round)

    await pp._tick(MagicMock(), sync_db_path)

    do_round.assert_not_awaited()


async def test_tick_skips_sweep_for_disabled_guild(sync_db_path, monkeypatch):
    _configure(sync_db_path, enabled=False)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
    do_round = AsyncMock(return_value=(1, 0))
    monkeypatch.setattr(pp, "_do_round", do_round)

    await pp._tick(MagicMock(), sync_db_path)

    do_round.assert_not_awaited()


# ── Abnormal session teardown ─────────────────────────────────────────
#
# A session that ends because a member was banned/left, or because a mod
# deleted the channel, must not silently drop the survivors: they go back in
# the pool, and the close never routes through the expiry path (so
# ``pen_pal_complete`` doesn't fire for an abandoned session).


def _close_reason(db_path, session_id: str) -> str | None:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT close_reason FROM pen_pals_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["close_reason"] if row else None


def test_close_abnormal_member_left_requeues_only_partner(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, now)
        row = pp._get_session_by_channel(conn, 4242)
        requeued = pp._close_abnormal_and_requeue(conn, row, "member_left", departed_user_id=1)

    assert requeued == [2]
    assert _pool_ids(sync_db_path) == [2]  # departed member 1 is not re-queued
    assert _active_session(sync_db_path, 1) is None
    assert _close_reason(sync_db_path, "s1") == "member_left"


def test_close_abnormal_channel_delete_requeues_both(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, now)
        row = pp._get_session_by_channel(conn, 4242)
        requeued = pp._close_abnormal_and_requeue(conn, row, "channel_deleted", departed_user_id=None)

    assert sorted(requeued) == [1, 2]
    assert sorted(_pool_ids(sync_db_path)) == [1, 2]
    assert _close_reason(sync_db_path, "s1") == "channel_deleted"


def test_close_abnormal_is_idempotent_on_double_event(sync_db_path):
    """The ban listener deletes the channel, which fires on_guild_channel_delete
    for the same session — only the first claim re-queues; the second is a no-op."""
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, now)
        row = pp._get_session_by_channel(conn, 4242)
        first = pp._close_abnormal_and_requeue(conn, row, "member_left", departed_user_id=1)
        second = pp._close_abnormal_and_requeue(conn, row, "channel_deleted", departed_user_id=None)

    assert first == [2]
    assert second is None                     # already closed → claim fails
    assert _pool_ids(sync_db_path) == [2]      # partner pooled exactly once
    assert _close_reason(sync_db_path, "s1") == "member_left"  # first reason wins


def test_close_abnormal_skips_survivor_already_pooled(sync_db_path):
    """A survivor already in the pool isn't added a second time."""
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, now)
        pp._add_to_pool(conn, GUILD_ID, 2)  # somehow already queued
        row = pp._get_session_by_channel(conn, 4242)
        requeued = pp._close_abnormal_and_requeue(conn, row, "member_left", departed_user_id=1)

    assert requeued == []                # nothing newly added
    assert _pool_ids(sync_db_path) == [2]  # still present, not duplicated


async def test_end_session_abnormally_deletes_channel_and_dms_survivor(sync_db_path, monkeypatch):
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, now)
        row = pp._get_session_by_channel(conn, 4242)

    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.delete = AsyncMock()
    bot.get_channel.return_value = channel

    await pp._end_session_abnormally(
        bot, sync_db_path, row, reason="member_left", departed_user_id=1, delete_channel=True,
    )

    channel.delete.assert_awaited_once()
    guild.get_member(2).send.assert_awaited_once()
    guild.get_member(1).send.assert_not_awaited()  # departed member isn't messaged
    assert _pool_ids(sync_db_path) == [2]


async def test_end_session_abnormally_second_call_is_noop(sync_db_path, monkeypatch):
    """The duplicate on_guild_channel_delete after a ban does nothing."""
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, now)
        row = pp._get_session_by_channel(conn, 4242)

    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.delete = AsyncMock()
    bot.get_channel.return_value = channel

    await pp._end_session_abnormally(
        bot, sync_db_path, row, reason="member_left", departed_user_id=1, delete_channel=True,
    )
    guild.get_member(2).send.reset_mock()

    # Second event for the same (now-closed) session.
    await pp._end_session_abnormally(
        bot, sync_db_path, row, reason="channel_deleted", departed_user_id=None, delete_channel=False,
    )

    guild.get_member(2).send.assert_not_awaited()
    assert _pool_ids(sync_db_path) == [2]  # not re-queued twice


async def test_on_member_remove_drops_pooled_member(sync_db_path, monkeypatch):
    """A member who was only in the pool (no session) is removed on leave."""
    refresh = AsyncMock()
    monkeypatch.setattr(pp, "_refresh_panel", refresh)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 7)

    ctx = MagicMock(db_path=sync_db_path)
    cog = pp.PenPalsCog(MagicMock(), ctx)
    member = MagicMock(spec=discord.Member, id=7)
    member.guild = MagicMock(id=GUILD_ID)

    await cog._on_member_remove(member)

    assert _pool_ids(sync_db_path) == []
    refresh.assert_awaited_once()
