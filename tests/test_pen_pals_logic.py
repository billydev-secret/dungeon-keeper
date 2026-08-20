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
- ``_pick_partner`` / ``_past_partners`` / ``_eligible_pool`` — the all-time
  no-repeat gate (never pair the same two twice; wait instead), oldest-first
  ordering, the one-chat-at-a-time exclusion, and the re-match cooldown's
  anchor (a session's *close*, not its start).
- ``_do_round`` — FIFO drain of whoever is left over, odd-one-out, failed
  pairs counted as waiting, the re-match cooldown, and the no-repeat gate.

Cooldown tests build finished sessions with ``_ended_session`` so both ends of
the chat are explicit — ``_close_session`` stamps ``closed_at`` with *now*,
which is the value the cooldown actually reads.

Discord objects are ``MagicMock(spec=...)`` so ``isinstance`` checks in the
cog pass without a gateway connection; the network-facing helpers
(``_create_channel``, ``_post_intro``, ``_refresh_panel``)
are monkeypatched at the module level.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs import pen_pals_cog as pp
from bot_modules.core.db_utils import open_db
from tests.fakes import FakeGuild, FakeMember, FakeRole, FakeUser, fake_interaction

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
    room_visibility: str = pp.DEFAULT_ROOM_VISIBILITY,
    intro_message: str = "",
    match_mode: str = pp.DEFAULT_MATCH_MODE,
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
            room_visibility=room_visibility,
            intro_message=intro_message,
            match_mode=match_mode,
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


def _make_bot_mock(
    guild: MagicMock,
    *,
    admin_role_ids: "frozenset[int] | set[int]" = frozenset(),
    mod_role_ids: "frozenset[int] | set[int]" = frozenset(),
) -> MagicMock:
    bot = MagicMock(spec=discord.Client)
    bot.get_guild.return_value = guild
    # _do_pair reads the guild's configured admin/mod roles via bot.ctx to size
    # room visibility overwrites.
    bot.ctx = MagicMock()
    bot.ctx.guild_config.return_value = SimpleNamespace(
        admin_role_ids=frozenset(admin_role_ids),
        mod_role_ids=frozenset(mod_role_ids),
    )
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

    async def fake_create_channel(
        guild_, category_, user1_, user2_, *,
        nsfw=False,
        visibility=pp.DEFAULT_ROOM_VISIBILITY,
        staff_role_ids=frozenset(),
    ):
        created.append({
            "nsfw": nsfw,
            "visibility": visibility,
            "staff_role_ids": set(staff_role_ids),
        })
        return channel

    monkeypatch.setattr(pp, "_create_channel", fake_create_channel)
    monkeypatch.setattr(pp, "_post_intro", AsyncMock())
    monkeypatch.setattr(pp, "resolve_accent_color", AsyncMock(return_value=None))
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
    assert not embed.description


async def test_post_intro_embed_uses_configured_intro_message_as_description():
    channel = MagicMock(spec=discord.TextChannel)
    intro_msg = MagicMock()
    intro_msg.pin = AsyncMock()
    channel.send = AsyncMock(side_effect=[intro_msg, MagicMock()])
    user1 = FakeUser(1, "Alice")
    user2 = FakeUser(2, "Bob")

    await pp._post_intro(
        channel, user1, user2, time.time() + 3600, "A question?",
        intro_message="Be kind to your pen pal!",
    )

    embed = channel.send.call_args_list[0].kwargs["embed"]
    assert embed.description == "Be kind to your pen pal!"


async def test_post_intro_embed_truncates_oversized_intro_message():
    channel = MagicMock(spec=discord.TextChannel)
    intro_msg = MagicMock()
    intro_msg.pin = AsyncMock()
    channel.send = AsyncMock(side_effect=[intro_msg, MagicMock()])
    user1 = FakeUser(1, "Alice")
    user2 = FakeUser(2, "Bob")

    await pp._post_intro(
        channel, user1, user2, time.time() + 3600, "A question?",
        intro_message="x" * 5000,
    )

    embed = channel.send.call_args_list[0].kwargs["embed"]
    assert embed.description == "x" * 4096


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


async def test_draw_question_prefers_bank(sync_db_path):
    _add_bank_question(sync_db_path, "from the bank?")
    q = await pp._draw_question(sync_db_path, "sess", False)
    assert q == "from the bank?"


async def test_draw_question_static_fallback_when_bank_empty(sync_db_path):
    """An empty bank yields the static question, not an AI call.

    The AI fallback was removed with the Prompts & AI studios; the module no
    longer imports ``generate_text`` at all, so a matched pair can never be
    handed an empty round.
    """
    assert not hasattr(pp, "generate_text")
    q = await pp._draw_question(sync_db_path, "sess", False)
    assert q == pp._FALLBACK_QUESTION


async def test_draw_question_excludes_session_history(sync_db_path):
    _add_bank_question(sync_db_path, "q1")
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


def test_past_partners_reads_both_sides(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "a", GUILD_ID, 1, 1, 2, now)
        pp._create_session(conn, "b", GUILD_ID, 2, 3, 1, now)
        assert pp._past_partners(conn, GUILD_ID, 1) == {2, 3}
        assert pp._past_partners(conn, GUILD_ID, 2) == {1}


def test_past_partners_is_all_time_not_a_recent_window(sync_db_path):
    """A partner from long ago and many pairings back still counts."""
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "ancient", GUILD_ID, 1, 1, 2, now - 400 * 86400)
        for i in range(20):  # 20 pairings since, well past any recency window
            pp._create_session(conn, f"s{i}", GUILD_ID, 100 + i, 1, 50 + i, now - i)
        assert 2 in pp._past_partners(conn, GUILD_ID, 1)


def test_past_partners_is_scoped_per_guild(sync_db_path):
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "elsewhere", GUILD_ID + 1, 1, 1, 2, time.time())
        assert pp._past_partners(conn, GUILD_ID, 1) == set()


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
    assert len(created) == 1 and created[0]["nsfw"] is False


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
    assert len(created) == 1 and created[0]["nsfw"] is True


async def test_do_pair_passes_configured_intro_message_to_post_intro(sync_db_path, pair_env):
    bot, _channel, _created = pair_env
    _configure(sync_db_path, intro_message="Be kind to your pen pal!")
    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is True
    assert pp._post_intro.call_args.kwargs["intro_message"] == "Be kind to your pen pal!"


async def test_do_pair_defaults_to_empty_intro_message(sync_db_path, pair_env):
    bot, _channel, _created = pair_env
    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is True
    assert pp._post_intro.call_args.kwargs["intro_message"] == ""


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


# ── Room visibility ───────────────────────────────────────────────────


def test_normalize_room_visibility_defaults_unknown_to_mods():
    assert pp.DEFAULT_ROOM_VISIBILITY == "mods"
    assert pp._normalize_room_visibility("admin") == "admin"
    assert pp._normalize_room_visibility("mods") == "mods"
    assert pp._normalize_room_visibility("everyone") == "everyone"
    assert pp._normalize_room_visibility(None) == "mods"
    assert pp._normalize_room_visibility("bogus") == "mods"


def test_room_staff_role_ids_scales_with_visibility():
    admins, mods = {10, 11}, {20, 21}
    # Admin-only: just the admin roles.
    assert pp._room_staff_role_ids("admin", admins, mods) == {10, 11}
    # +mods: admin roles and mod roles.
    assert pp._room_staff_role_ids("mods", admins, mods) == {10, 11, 20, 21}
    # everyone: admins still listed so they keep post/manage when @everyone is
    # view-only; mods fold into @everyone.
    assert pp._room_staff_role_ids("everyone", admins, mods) == {10, 11}
    # Unknown value takes the default (mods) behavior.
    assert pp._room_staff_role_ids("bogus", admins, mods) == {10, 11, 20, 21}


def test_room_everyone_can_view_only_for_everyone():
    assert pp._room_everyone_can_view("everyone") is True
    assert pp._room_everyone_can_view("mods") is False
    assert pp._room_everyone_can_view("admin") is False


def test_room_footer_text_names_the_audience():
    assert pp._room_footer_text("admin") == "Admins can see this channel."
    assert "mods" in pp._room_footer_text("mods").lower()
    assert "everyone" in pp._room_footer_text("everyone").lower()


def _overwrite_guild(role_ids: set[int]):
    """A guild whose get_role resolves the given ids to distinct role mocks."""
    guild = MagicMock(spec=discord.Guild)
    guild.default_role = MagicMock(spec=discord.Role, id=0)
    guild.me = MagicMock(spec=discord.Member, id=999)
    roles = {rid: MagicMock(spec=discord.Role, id=rid) for rid in role_ids}
    guild.get_role.side_effect = roles.get
    u1 = MagicMock(spec=discord.Member, id=1)
    u2 = MagicMock(spec=discord.Member, id=2)
    return guild, roles, u1, u2


def test_room_overwrites_admin_hides_room_and_grants_admin_roles():
    guild, roles, u1, u2 = _overwrite_guild({10})
    ow = pp._room_overwrites(guild, u1, u2, visibility="admin", staff_role_ids={10})
    assert ow[guild.default_role].view_channel is False   # @everyone can't see
    assert ow[roles[10]].view_channel is True             # admin role can
    assert ow[u1].send_messages is True and ow[u2].send_messages is True


def test_room_overwrites_mods_grants_the_mod_role_view_and_history():
    guild, roles, u1, u2 = _overwrite_guild({10, 20})
    ow = pp._room_overwrites(guild, u1, u2, visibility="mods", staff_role_ids={10, 20})
    assert ow[guild.default_role].view_channel is False
    assert ow[roles[20]].view_channel is True
    assert ow[roles[20]].read_message_history is True


def test_room_overwrites_everyone_is_readonly_public():
    guild, _roles, u1, u2 = _overwrite_guild(set())
    ow = pp._room_overwrites(guild, u1, u2, visibility="everyone", staff_role_ids=set())
    everyone = ow[guild.default_role]
    assert everyone.view_channel is True          # world-readable
    assert everyone.send_messages is False        # but watch-only
    assert everyone.read_message_history is True
    assert ow[u1].send_messages is True           # the pair can still post


def test_room_overwrites_skips_unresolvable_role_ids():
    guild, roles, u1, u2 = _overwrite_guild({10})  # 10 resolves; 99 does not
    ow = pp._room_overwrites(guild, u1, u2, visibility="mods", staff_role_ids={10, 99})
    assert roles[10] in ow
    assert all(getattr(k, "id", None) != 99 for k in ow)  # skipped, no crash


async def _capture_do_pair(
    sync_db_path, monkeypatch, *, room_visibility, admin_role_ids, mod_role_ids
) -> dict:
    """Run _do_pair with the given config/roles; return the _create_channel kwargs."""
    _configure(sync_db_path, room_visibility=room_visibility)
    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild, admin_role_ids=admin_role_ids, mod_role_ids=mod_role_ids)
    captured: dict = {}

    async def fake_create_channel(
        g, c, u1, u2, *, nsfw=False,
        visibility=pp.DEFAULT_ROOM_VISIBILITY, staff_role_ids=frozenset(),
    ):
        captured["visibility"] = visibility
        captured["staff_role_ids"] = set(staff_role_ids)
        ch = MagicMock(spec=discord.TextChannel, id=55, mention="#x")
        ch.delete = AsyncMock()
        return ch

    monkeypatch.setattr(pp, "_create_channel", fake_create_channel)
    monkeypatch.setattr(pp, "_post_intro", AsyncMock())
    monkeypatch.setattr(pp, "resolve_accent_color", AsyncMock(return_value=None))
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1)
        pp._add_to_pool(conn, GUILD_ID, 2)
    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2) is True
    return captured


async def test_do_pair_mods_visibility_grants_admin_and_mod_roles(sync_db_path, monkeypatch):
    captured = await _capture_do_pair(
        sync_db_path, monkeypatch,
        room_visibility="mods", admin_role_ids={10}, mod_role_ids={20},
    )
    assert captured["visibility"] == "mods"
    assert captured["staff_role_ids"] == {10, 20}


async def test_do_pair_admin_visibility_excludes_mod_roles(sync_db_path, monkeypatch):
    captured = await _capture_do_pair(
        sync_db_path, monkeypatch,
        room_visibility="admin", admin_role_ids={10}, mod_role_ids={20},
    )
    assert captured["visibility"] == "admin"
    assert captured["staff_role_ids"] == {10}  # mod role NOT granted


def test_set_config_round_trips_room_visibility(sync_db_path):
    _configure(sync_db_path, room_visibility="everyone")
    with open_db(sync_db_path) as conn:
        assert pp._get_config(conn, GUILD_ID)["room_visibility"] == "everyone"


def test_set_config_normalizes_bad_room_visibility_to_default(sync_db_path):
    _configure(sync_db_path, room_visibility="nonsense")
    with open_db(sync_db_path) as conn:
        assert pp._get_config(conn, GUILD_ID)["room_visibility"] == "mods"


def test_set_config_round_trips_match_mode(sync_db_path):
    _configure(sync_db_path, match_mode="scheduled")
    with open_db(sync_db_path) as conn:
        assert pp._get_config(conn, GUILD_ID)["match_mode"] == "scheduled"


def test_set_config_normalizes_bad_match_mode_to_instant(sync_db_path):
    _configure(sync_db_path, match_mode="nonsense")
    with open_db(sync_db_path) as conn:
        assert pp._get_config(conn, GUILD_ID)["match_mode"] == "instant"


def test_set_config_defaults_match_mode_to_instant(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        assert pp._get_config(conn, GUILD_ID)["match_mode"] == "instant"


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


def _ended_session(
    conn,
    session_id: str,
    user1_id: int,
    user2_id: int,
    *,
    started_at: float,
    closed_at: float,
    guild_id: int = GUILD_ID,
) -> None:
    """A finished session whose start *and* end are both explicit.

    ``_close_session`` stamps ``closed_at`` with *now*, which is the value the
    re-match cooldown reads — so a test that backdates only ``started_at`` and
    then closes is describing a chat that ended this instant. Cooldown tests
    have to set both ends.
    """
    pp._create_session(conn, session_id, guild_id, 99, user1_id, user2_id, started_at)
    conn.execute(
        "UPDATE pen_pals_sessions SET state = 'closed', closed_at = ?, close_reason = 'expired' "
        "WHERE session_id = ?",
        (closed_at, session_id),
    )


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
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
        # 2's chat ended an hour ago — well inside the 2-day cooldown.
        _ended_session(conn, "recent", 2, 3, started_at=now - 7200, closed_at=now - 3600)
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    await pp._handle_join(_join_interaction(1), sync_db_path)

    do_pair.assert_not_awaited()
    assert _pool_ids(sync_db_path) == [2, 1]


async def test_handle_join_skips_when_joiner_is_on_cooldown(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 172800)
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
        # The joiner's own chat ended an hour ago — inside the 2-day cooldown.
        _ended_session(conn, "recent", 1, 3, started_at=now - 7200, closed_at=now - 3600)
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


async def test_handle_join_scheduled_mode_never_matches_on_join(sync_db_path, monkeypatch):
    """Scheduled mode queues everyone — even with a partner waiting, join never pairs."""
    _configure(sync_db_path, match_mode="scheduled")
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    do_pair.assert_not_awaited()
    assert set(_pool_ids(sync_db_path)) == {1, 2}
    msg = interaction.response.send_message.await_args.args[0]
    assert "once a day at 8:00 AM Eastern" in msg


# ── _pick_partner / _eligible_pool ────────────────────────────────────


def test_pick_partner_takes_oldest_waiter_who_is_not_a_past_partner():
    assert pp._pick_partner([2, 3, 4], {2, 3}) == 4


def test_pick_partner_returns_none_when_every_waiter_is_a_past_partner():
    """No-repeat is a hard gate: waiting beats handing back the same person."""
    assert pp._pick_partner([2, 3], {2, 3}) is None


def test_pick_partner_returns_none_for_empty_pool():
    assert pp._pick_partner([], set()) is None


def test_eligible_pool_excludes_members_already_in_a_session(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
        pp._create_session(conn, "busy", GUILD_ID, 99, 2, 3, time.time())
        assert pp._eligible_pool(conn, GUILD_ID, time.time(), 0) == [1]


def test_eligible_pool_cooldown_runs_from_when_the_chat_ended(sync_db_path):
    """Regression: the cooldown was anchored to ``started_at``.

    That made it tick *during* the session, so a member's real rest was
    ``cooldown - session_length`` — and a cooldown shorter than the session
    length was no rest at all. Here the chat ran 3 hours and ended 30 minutes
    ago under a 1-hour cooldown: still resting, even though 3h has passed
    since it began.
    """
    _configure(sync_db_path)
    now = time.time()
    with open_db(sync_db_path) as conn:
        _ended_session(conn, "s", 1, 99, started_at=now - 10800, closed_at=now - 1800)
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
        assert pp._eligible_pool(conn, GUILD_ID, now, 3600) == [2]


def test_eligible_pool_frees_a_member_once_the_cooldown_clears_the_close(sync_db_path):
    """The other side of the same anchor: rest is measured whole, then over."""
    _configure(sync_db_path)
    now = time.time()
    with open_db(sync_db_path) as conn:
        _ended_session(conn, "s", 1, 99, started_at=now - 18000, closed_at=now - 7200)
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
        assert pp._eligible_pool(conn, GUILD_ID, now, 3600) == [1, 2]


def test_last_pen_pal_ended_at_falls_back_to_start_for_an_unfinished_session(sync_db_path):
    """``closed_at`` is NULL while a chat is open, and NULL must not read as
    "never had a pen pal" — an open session anchors to its own start."""
    _configure(sync_db_path)
    now = time.time()
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "open", GUILD_ID, 99, 1, 2, now - 600)
        assert pp._last_pen_pal_ended_at(conn, GUILD_ID, 1) == pytest.approx(now - 600)
        assert pp._last_pen_pal_ended_at(conn, GUILD_ID, 3) is None


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
    """Leaving with no pool row to delete is not an error any more.

    It used to answer "❌ You're not in the pool", which was true of the row
    and false of the intent — and the state they asked for is now something
    the bot can actually hold, so it holds it.
    """
    _configure(sync_db_path)
    interaction = _join_interaction(1)
    await pp._handle_leave(interaction, sync_db_path)
    msg = interaction.response.send_message.await_args.args[0]
    assert "❌" not in msg
    assert "won't be matched" in msg
    with open_db(sync_db_path) as conn:
        assert pp._is_opted_out(conn, GUILD_ID, 1)


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


async def test_do_round_steers_away_from_a_past_partner(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    now = time.time()
    with open_db(sync_db_path) as conn:
        # 1 and 2 were paired before — but their chat *ended* long enough ago to
        # clear the month-long cooldown, so all three are eligible and only the
        # no-repeat gate should steer 1 away from 2. 3 is fresh.
        _ended_session(
            conn, "old", 1, 2,
            started_at=now - (_COOLDOWN + 2 * 86400),
            closed_at=now - (_COOLDOWN + 86400),
        )
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


async def test_do_round_never_repairs_the_only_two_waiting(sync_db_path, monkeypatch):
    """Regression: a sweep re-paired two members the moment their cooldown lapsed.

    The cooldown runs from the *shared* session's close, so both halves of a
    pair come off it at the same instant and float back into a pool holding
    nobody else. Before the no-repeat gate was hard, ``_pick_partner`` ran out of
    non-past candidates and fell back to the oldest waiter — the same person.
    """
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 172800)  # 2 days, as the live guild runs it
    now = time.time()
    with open_db(sync_db_path) as conn:
        _ended_session(
            conn, "yesterday", 1, 2,
            started_at=now - (172800 + 86400),
            closed_at=now - (172800 + 60),  # cooldown lapsed a minute ago
        )
        for i, uid in enumerate([1, 2], start=1):
            pp._add_to_pool(conn, GUILD_ID, uid, joined_at=float(i))

    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    pairs, waiting = await pp._do_round(MagicMock(), sync_db_path, GUILD_ID)

    do_pair.assert_not_awaited()
    assert pairs == 0 and waiting == 2
    assert _pool_ids(sync_db_path) == [1, 2]  # both wait for someone new


async def test_do_round_pairs_past_partners_onward_to_fresh_faces(sync_db_path, monkeypatch):
    """The gate defers a repeat, it doesn't strand anyone: 3 joins, both match."""
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "old12", GUILD_ID, 5, 1, 2, time.time() - 86400)
        pp._close_session(conn, "old12", "expired")
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
    assert calls == [(1, 3), (2, 4)]
    assert pairs == 2 and waiting == 0


async def test_do_round_skips_members_matched_within_the_month(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    now = time.time()
    with open_db(sync_db_path) as conn:
        # 1's chat ended six days ago → still cooling down; 2 and 3 are fresh.
        _ended_session(
            conn, "recent", 1, 99,
            started_at=now - 7 * 86400, closed_at=now - 6 * 86400,
        )
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
        # 1's chat ended six days ago — still inside the hardcoded 30-day
        # default, but well past the guild's configured 1-hour cooldown.
        now = time.time()
        _ended_session(
            conn, "recent", 1, 99,
            started_at=now - 7 * 86400, closed_at=now - 6 * 86400,
        )
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
    now = time.time()
    with open_db(sync_db_path) as conn:
        # Both chats ended just over a month ago → both eligible again.
        started, closed = now - (_COOLDOWN + 2 * 86400), now - (_COOLDOWN + 86400)
        _ended_session(conn, "a", 1, 98, started_at=started, closed_at=closed)
        _ended_session(conn, "b", 2, 97, started_at=started, closed_at=closed)
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


def test_find_instant_match_excludes_a_past_partner(sync_db_path):
    """Joining can't hand you back someone you've already had, ever."""
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "old12", GUILD_ID, 5, 1, 2, time.time() - 86400)
        pp._close_session(conn, "old12", "expired")
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
        assert pp._find_instant_match(conn, GUILD_ID, 1) is None
        # A member they haven't had is still matched on the spot.
        pp._add_to_pool(conn, GUILD_ID, 3, joined_at=200.0)
        assert pp._find_instant_match(conn, GUILD_ID, 1) == 3


async def test_handle_join_queues_when_only_candidate_is_a_past_partner(
    sync_db_path, monkeypatch
):
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "old12", GUILD_ID, 5, 1, 2, time.time() - 86400)
        pp._close_session(conn, "old12", "expired")
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=100.0)
    do_pair = AsyncMock(return_value=True)
    monkeypatch.setattr(pp, "_do_pair", do_pair)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    do_pair.assert_not_awaited()
    assert _pool_ids(sync_db_path) == [2, 1]
    assert "in the pool" in interaction.response.send_message.await_args.args[0]


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


def test_force_pair_status_requires_both_members_opted_in(sync_db_path):
    """Force-pairing is consent-gated: both sides must be in the pool."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        assert pp._force_pair_status(conn, GUILD_ID, 10, 20) == "not_opted_in_1"
        pp._add_to_pool(conn, GUILD_ID, 10)
        assert pp._force_pair_status(conn, GUILD_ID, 10, 20) == "not_opted_in_2"
        pp._add_to_pool(conn, GUILD_ID, 20)
        assert pp._force_pair_status(conn, GUILD_ID, 10, 20) == "ok"


def test_force_pair_status_reports_disabled_blocked_and_active(sync_db_path):
    """The other refusal reasons keep their precedence over the opt-in gate."""
    with open_db(sync_db_path) as conn:
        assert pp._force_pair_status(conn, GUILD_ID, 10, 20) == "disabled"
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_block(conn, GUILD_ID, 10, 20)
        assert pp._force_pair_status(conn, GUILD_ID, 10, 20) == "blocked"
        pp._remove_block(conn, GUILD_ID, 10, 20)
        pp._add_to_pool(conn, GUILD_ID, 10)
        pp._add_to_pool(conn, GUILD_ID, 20)
        pp._create_session(conn, "s-active", GUILD_ID, 9001, 20, 30, time.time())
        assert pp._force_pair_status(conn, GUILD_ID, 10, 20) == "active_2"
        assert pp._force_pair_status(conn, GUILD_ID, 20, 10) == "active_1"


async def test_penpals_pair_command_refuses_a_member_who_never_opted_in(sync_db_path):
    """A mod can't drop a member who never joined the pool into a pen pal channel."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 10)  # only user1 opted in

    ctx = MagicMock(db_path=sync_db_path)
    cog = pp.PenPalsCog(MagicMock(), ctx)
    g = FakeGuild(id=GUILD_ID)
    u1 = FakeUser(id=10, display_name="Ten")
    u2 = FakeUser(id=20, display_name="Twenty")
    interaction = fake_interaction(user=FakeUser(id=1), guild=g)

    await cog.penpals_pair.callback(cog, interaction, u1, u2)

    msg = interaction.response.send_message.await_args.args[0]
    assert "Twenty" in msg  # named by display name…
    assert u2.mention not in msg  # …never pinged
    assert "opted in" in msg.lower()
    interaction.response.defer.assert_not_awaited()


# ── Panel refresh delegation ──────────────────────────────────────────
#
# The panel now runs on core.sticky.StickyPanel, which owns the per-guild lock
# (serialisation is covered by tests/test_core_sticky.py). What matters here is
# that pen pals routes the two intents to the right place.


async def test_refresh_panel_edits_in_place_by_default(sync_db_path):
    cog = MagicMock()
    cog.panel.refresh = AsyncMock()
    cog.repost_panel = AsyncMock()
    bot = MagicMock()
    bot.get_cog.return_value = cog

    await pp._refresh_panel(bot, GUILD_ID)

    cog.panel.refresh.assert_awaited_once_with(GUILD_ID)
    cog.repost_panel.assert_not_awaited()


async def test_refresh_panel_repost_moves_it_to_the_bottom(sync_db_path):
    cog = MagicMock()
    cog.panel.refresh = AsyncMock()
    cog.repost_panel = AsyncMock()
    bot = MagicMock()
    bot.get_cog.return_value = cog

    await pp._refresh_panel(bot, GUILD_ID, repost=True)

    cog.repost_panel.assert_awaited_once_with(GUILD_ID)
    cog.panel.refresh.assert_not_awaited()


async def test_refresh_panel_noop_when_the_cog_is_unloaded(sync_db_path):
    bot = MagicMock()
    bot.get_cog.return_value = None
    await pp._refresh_panel(bot, GUILD_ID)  # must not raise


async def test_panel_ids_are_zero_without_config(sync_db_path):
    """No pen_pals_config row: the panel reads as unposted, so nothing sticks."""
    cog = pp.PenPalsCog.__new__(pp.PenPalsCog)
    cog.ctx = SimpleNamespace(db_path=sync_db_path)
    assert cog._panel_ids(GUILD_ID) == (0, 0)


# ── Signup panel embed ──────────────────────────────────────────────


def test_panel_embed_instant_mode_describes_matching_on_the_spot():
    embed = pp._build_panel_embed(3, mode="instant")
    assert "matched on the spot" in embed.description
    assert "8:00 AM Eastern" not in embed.description


def test_panel_embed_scheduled_mode_describes_daily_round():
    embed = pp._build_panel_embed(3, mode="scheduled")
    assert "8:00 AM Eastern" in embed.description
    assert "matched on the spot" not in embed.description


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


# ── _scheduled_round_due ────────────────────────────────────────────


def _et(y: int, m: int, d: int, h: int, mi: int = 0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=pp._SCHEDULED_MATCH_TZ).timestamp()


def test_scheduled_round_not_due_before_8am_local():
    cfg = {"last_auto_round_at": 0}
    assert pp._scheduled_round_due(cfg, _et(2026, 7, 24, 7, 59)) is False


def test_scheduled_round_due_at_8am_if_never_run():
    cfg = {"last_auto_round_at": 0}
    assert pp._scheduled_round_due(cfg, _et(2026, 7, 24, 8, 0)) is True


def test_scheduled_round_not_due_again_same_local_day():
    cfg = {"last_auto_round_at": _et(2026, 7, 24, 8, 3)}
    assert pp._scheduled_round_due(cfg, _et(2026, 7, 24, 14, 0)) is False


def test_scheduled_round_due_again_the_next_local_day():
    cfg = {"last_auto_round_at": _et(2026, 7, 24, 8, 3)}
    assert pp._scheduled_round_due(cfg, _et(2026, 7, 25, 8, 1)) is True


def test_scheduled_round_catches_up_after_bot_downtime_past_8am():
    """A round missed at 8am (e.g. the bot was offline) still runs later that day."""
    cfg = {"last_auto_round_at": _et(2026, 7, 23, 8, 5)}
    assert pp._scheduled_round_due(cfg, _et(2026, 7, 24, 20, 0)) is True


# ── _tick scheduled-mode round ────────────────────────────────────────


async def test_tick_runs_scheduled_round_when_due(sync_db_path, monkeypatch):
    _configure(sync_db_path, match_mode="scheduled")
    monkeypatch.setattr(pp.time, "time", lambda: _et(2026, 7, 24, 8, 30))
    do_round = AsyncMock(return_value=(1, 0))
    monkeypatch.setattr(pp, "_do_round", do_round)

    await pp._tick(MagicMock(), sync_db_path)

    assert do_round.await_args.args[2] == GUILD_ID


async def test_tick_skips_scheduled_round_before_8am(sync_db_path, monkeypatch):
    _configure(sync_db_path, match_mode="scheduled")
    monkeypatch.setattr(pp.time, "time", lambda: _et(2026, 7, 24, 7, 59))
    do_round = AsyncMock(return_value=(0, 0))
    monkeypatch.setattr(pp, "_do_round", do_round)

    await pp._tick(MagicMock(), sync_db_path)

    do_round.assert_not_awaited()


async def test_tick_skips_scheduled_round_already_run_today(sync_db_path, monkeypatch):
    _configure(sync_db_path, match_mode="scheduled")
    with open_db(sync_db_path) as conn:
        conn.execute(
            "UPDATE pen_pals_config SET last_auto_round_at = ? WHERE guild_id = ?",
            (_et(2026, 7, 24, 8, 5), GUILD_ID),
        )
    monkeypatch.setattr(pp.time, "time", lambda: _et(2026, 7, 24, 14, 0))
    do_round = AsyncMock(return_value=(0, 0))
    monkeypatch.setattr(pp, "_do_round", do_round)

    await pp._tick(MagicMock(), sync_db_path)

    do_round.assert_not_awaited()


async def test_tick_scheduled_round_runs_even_with_fewer_than_two_pending(sync_db_path, monkeypatch):
    """Scheduled mode always draws at 8am ET, even to just stamp a 0- or 1-person pool

    — unlike instant mode's sweep, which skips guilds with fewer than two eligible
    members so it doesn't spin every 5 minutes on an empty pool.
    """
    _configure(sync_db_path, match_mode="scheduled")
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
    monkeypatch.setattr(pp.time, "time", lambda: _et(2026, 7, 24, 8, 30))
    do_round = AsyncMock(return_value=(0, 1))
    monkeypatch.setattr(pp, "_do_round", do_round)

    await pp._tick(MagicMock(), sync_db_path)

    do_round.assert_awaited_once()


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


# ── Close ordering vs. our own CHANNEL_DELETE ─────────────────────────
#
# Every normal close deletes the session's channel, and Discord hands that
# delete straight back as CHANNEL_DELETE. While the row was still active at
# that moment, _on_channel_delete read it as an *abandoned* session: both
# members were DM'd "your partner is no longer available" and re-pooled on a
# perfectly normal completion. Claiming the row first makes the listener's
# lookup miss, so these tests fire the event from inside channel.delete().


def _echo_channel_delete(bot, db_path, channel_id: int):
    """A channel.delete() that dispatches _on_channel_delete's body, as Discord does."""
    async def _fire(*_args, **_kwargs):
        with open_db(db_path) as conn:
            session = pp._get_session_by_channel(conn, channel_id)
        if session is None:
            return
        await pp._end_session_abnormally(
            bot, db_path, session,
            reason="channel_deleted", departed_user_id=None, delete_channel=False,
        )
    return _fire


async def test_tick_expiry_ignores_the_delete_event_it_causes(sync_db_path, monkeypatch):
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    fire = AsyncMock()
    monkeypatch.setattr("bot_modules.economy.game_rewards.fire_member_trigger", fire)
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._create_session(
            conn, "s1", GUILD_ID, 4242, 1, 2, time.time() - 10, session_seconds=0,
        )

    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.delete = AsyncMock(side_effect=_echo_channel_delete(bot, sync_db_path, 4242))
    bot.get_channel.return_value = channel

    await pp._tick(bot, sync_db_path)

    assert _close_reason(sync_db_path, "s1") == "expired"
    assert _pool_ids(sync_db_path) == []            # silent chat — neither re-queued
    for uid in (1, 2):
        send = guild.get_member(uid).send
        assert send.await_count == 1                # the closing note, and only it
        body = send.await_args.kwargs["embed"].description
        assert "no longer available" not in body    # never "your partner vanished"
        assert "unqueued you" in body
    assert fire.await_count == 2                    # the completion still counts


async def test_end_early_ignores_the_delete_event_it_causes(sync_db_path, monkeypatch):
    """/penpals end DMs the partner once — not again as an abandoned session."""
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, time.time())

    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.guild = guild
    channel.delete = AsyncMock(side_effect=_echo_channel_delete(bot, sync_db_path, 4242))

    view = pp._EndConfirmView(sync_db_path, "s1", channel, other_user_id=2, invoker_id=1)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(id=1)
    interaction.response.edit_message = AsyncMock()

    await view.confirm.callback(interaction)

    assert _close_reason(sync_db_path, "s1") == "early"
    assert _pool_ids(sync_db_path) == []
    guild.get_member(2).send.assert_awaited_once()  # "ended early by your partner", once
    guild.get_member(1).send.assert_not_awaited()


# ── Orphan-channel sweep ──────────────────────────────────────────────
#
# The flip side of closing the row first: a crash (or a failed delete) between
# the two leaves a live channel behind a closed row, and the tick only walks
# *active* sessions. The sweep is what revisits it.


def _sweep_env(db_path, *, close: bool):
    with open_db(db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, time.time())
        if close:
            pp._claim_close(conn, "s1", "expired")

    channel = MagicMock(spec=discord.TextChannel)
    channel.delete = AsyncMock()
    bot = MagicMock(spec=discord.Client)
    bot.get_channel.return_value = channel
    return bot, channel


async def test_sweep_deletes_a_channel_left_behind_by_a_closed_session(sync_db_path):
    bot, channel = _sweep_env(sync_db_path, close=True)

    await pp._sweep_orphan_channels(bot, sync_db_path)

    channel.delete.assert_awaited_once()


async def test_sweep_leaves_a_live_session_alone(sync_db_path):
    bot, channel = _sweep_env(sync_db_path, close=False)

    await pp._sweep_orphan_channels(bot, sync_db_path)

    channel.delete.assert_not_awaited()


async def test_sweep_still_finds_an_orphan_after_days_of_downtime(sync_db_path):
    """The hazard is a crash, and a crash is what keeps the bot down for days.

    A "recent closes only" window would go blind in exactly the case the sweep
    exists for, so the scan is unbounded in time.
    """
    bot, channel = _sweep_env(sync_db_path, close=True)
    with open_db(sync_db_path) as conn:  # closed a fortnight ago
        conn.execute(
            "UPDATE pen_pals_sessions SET closed_at = ? WHERE session_id = 's1'",
            (time.time() - 14 * 86400,),
        )

    await pp._sweep_orphan_channels(bot, sync_db_path)

    channel.delete.assert_awaited_once()


async def test_sweep_ignores_a_channel_discord_already_dropped(sync_db_path):
    """A stale cache entry is the benign case — not worth a warning per tick."""
    bot, channel = _sweep_env(sync_db_path, close=True)
    channel.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))

    await pp._sweep_orphan_channels(bot, sync_db_path)  # must not raise

    channel.delete.assert_awaited_once()


async def test_tick_survives_a_sweep_that_blows_up(sync_db_path, monkeypatch):
    """Maintenance runs last and swallows everything.

    channel.delete() can raise outside the HTTPException family (a transport
    error), and _pen_pals_loop's own catch is per-tick — an escape here would
    cost that cycle its expiries, close warnings and reminders.
    """
    _configure(sync_db_path)
    boom = AsyncMock(side_effect=OSError("connection reset"))
    monkeypatch.setattr(pp, "_sweep_orphan_channels", boom)

    await pp._tick(MagicMock(), sync_db_path)  # must not raise

    boom.assert_awaited_once()


async def test_on_member_remove_drops_pooled_member(sync_db_path, monkeypatch):
    """A member who was only in the pool (no session) is removed on leave, and
    the panel is refreshed so its pool count is accurate."""
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 7)

    ctx = MagicMock(db_path=sync_db_path)
    cog = pp.PenPalsCog(MagicMock(), ctx)
    # The cog holds its own StickyPanel now, rather than bouncing through the
    # module-level helper to reach itself.
    monkeypatch.setattr(cog.panel, "refresh", AsyncMock())
    member = MagicMock(spec=discord.Member, id=7)
    member.guild = MagicMock(id=GUILD_ID)

    await cog._on_member_remove(member)

    assert _pool_ids(sync_db_path) == []
    cog.panel.refresh.assert_awaited_once()


# ── Reply reminders ───────────────────────────────────────────────────
#
# The rule under test: one member has said something, the other hasn't
# answered for reply_reminder_seconds, so the quiet one gets pinged — once
# per silence. The "once" is enforced by comparing the stamp against the
# last member message rather than clearing it on reply, so the re-arm case
# below is the one that matters most.

_BASE_TS = 1_700_000_000.0
_SIX_H = 6 * 3600
_NOW = _BASE_TS + 7 * 3600  # 7h after the baseline: past a 6h threshold
_BOT_ID = 999
_ROOM_ID = 4242


def _set_reply_reminder(db_path, seconds: int, guild_id: int = GUILD_ID) -> None:
    with open_db(db_path) as conn:
        pp._set_timers(
            conn,
            guild_id,
            session_seconds=86400,
            match_cooldown_seconds=_COOLDOWN,
            max_question_swaps=3,
            warn_seconds=3600,
            question_suppress_seconds=7200,
            reply_reminder_seconds=seconds,
        )


def _say(
    db_path, author_id: int, offset: float, *, base: float = _BASE_TS,
    channel_id: int = _ROOM_ID,
) -> None:
    """Record a message in the room's log, `offset` seconds after `base`."""
    ts = base + offset
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(ts * 1000) + author_id, GUILD_ID, channel_id, author_id, int(ts)),
        )


def _session_for_reminder(
    db_path, *, stamp: float | None = None, base: float = _BASE_TS
) -> None:
    with open_db(db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, _ROOM_ID, 1, 2, base)
        if stamp is not None:
            pp._set_reply_reminder_sent(conn, "s1", base + stamp)


def _due(db_path, *, now: float = _NOW, seconds: int = _SIX_H) -> int | None:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM pen_pals_sessions WHERE session_id = 's1'"
        ).fetchone()
        return pp._reply_reminder_due(conn, row, now, seconds)


@pytest.mark.parametrize(
    ("says", "stamp", "seconds", "expected"),
    [
        pytest.param([(1, 0)], None, _SIX_H, 2, id="quiet_partner_is_the_one_pinged"),
        pytest.param([(2, 0)], None, _SIX_H, 1, id="either_side_can_be_the_quiet_one"),
        pytest.param(
            [(1, 0), (2, 60)], None, _SIX_H, 1, id="the_latest_speaker_decides_who_owes",
        ),
        pytest.param([(1, 3600)], None, _SIX_H, 2, id="fires_exactly_on_the_threshold"),
        pytest.param([(1, 3660)], None, _SIX_H, None, id="still_inside_the_quiet_window"),
        pytest.param([], None, _SIX_H, None, id="nobody_has_spoken_yet"),
        pytest.param([(_BOT_ID, 0)], None, _SIX_H, None, id="a_bot_post_is_nobody_waiting"),
        pytest.param([(1, 0)], 60, _SIX_H, None, id="this_silence_already_got_its_nudge"),
        pytest.param(
            [(1, 0), (2, 120), (1, 180)], 60, _SIX_H, 2, id="re_arms_after_they_reply",
        ),
        pytest.param([(1, 0)], None, 0, None, id="zero_disables_reminders"),
    ],
)
def test_reply_reminder_due_cases(sync_db_path, says, stamp, seconds, expected):
    _session_for_reminder(sync_db_path, stamp=stamp)
    for author_id, offset in says:
        _say(sync_db_path, author_id, offset)

    assert _due(sync_db_path, seconds=seconds) == expected


def test_reply_reminder_stops_once_the_closing_warning_is_out(sync_db_path):
    """No "reply!" stacked on top of "this chat closes in an hour"."""
    _session_for_reminder(sync_db_path)
    _say(sync_db_path, 1, 0)
    assert _due(sync_db_path) == 2

    with open_db(sync_db_path) as conn:
        pp._set_close_warning_sent(conn, "s1")

    assert _due(sync_db_path) is None


def test_reply_reminder_skips_a_pair_blocked_mid_session(sync_db_path):
    """A block landing mid-chat doesn't end the session, so the nudge itself
    has to stay quiet — pinging someone toward a partner they just blocked is
    exactly what the blocklist exists to prevent."""
    _session_for_reminder(sync_db_path)
    _say(sync_db_path, 1, 0)

    with open_db(sync_db_path) as conn:
        pp._add_block(conn, GUILD_ID, 2, 1)

    assert _due(sync_db_path) is None


def test_reply_reminder_skips_a_no_contact_pair(sync_db_path):
    """Same for a moderator-set no-contact entry (migration 146)."""
    from bot_modules.services import no_contact_service

    _session_for_reminder(sync_db_path)
    _say(sync_db_path, 1, 0)
    no_contact_service.add_pair(sync_db_path, GUILD_ID, 1, 2, created_by=5)

    assert _due(sync_db_path) is None


def test_reply_reminder_is_off_by_default_for_an_existing_guild(sync_db_path):
    """Migration 147 defaults the dial to 0 — no guild starts nudging unasked."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        assert pp._get_config(conn, GUILD_ID)["reply_reminder_seconds"] == 0


def test_set_timers_round_trips_the_reply_reminder(sync_db_path):
    _set_reply_reminder(sync_db_path, 8 * 3600)
    with open_db(sync_db_path) as conn:
        assert pp._get_config(conn, GUILD_ID)["reply_reminder_seconds"] == 8 * 3600


# ── _tick reply reminder ──────────────────────────────────────────────


def _reminder_env(db_path, *, seconds: int = _SIX_H):
    """A configured guild, one live session, a mocked room, and its baseline.

    The session opened 7h ago in real time (``_tick`` reads the clock itself),
    so a message posted at the baseline is already past a 6h threshold while
    the room is nowhere near expiry.
    """
    base = time.time() - 7 * 3600
    _configure(db_path)
    _set_reply_reminder(db_path, seconds)
    _session_for_reminder(db_path, base=base)

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = _ROOM_ID
    channel.send = AsyncMock()
    bot = MagicMock(spec=discord.Client)
    bot.get_channel.return_value = channel
    return bot, channel, base


def _stamp_of(db_path, session_id: str = "s1") -> float:
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT reply_reminder_sent_at FROM pen_pals_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]


async def test_tick_nudges_only_the_member_who_owes_a_reply(sync_db_path):
    bot, channel, base = _reminder_env(sync_db_path)
    _say(sync_db_path, 1, 0, base=base)

    await pp._tick(bot, sync_db_path)

    text = channel.send.await_args.args[0]
    assert "<@2>" in text and "<@1>" not in text
    # Allow-listed to the quiet member alone: no @everyone, no roles, and the
    # partner is referred to in prose rather than pinged.
    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert [o.id for o in mentions.users] == [2]
    assert mentions.everyone is False and mentions.roles is False
    assert _stamp_of(sync_db_path) == pytest.approx(time.time(), abs=30)


async def test_tick_does_not_nudge_twice_for_the_same_silence(sync_db_path):
    bot, channel, base = _reminder_env(sync_db_path)
    _say(sync_db_path, 1, 0, base=base)

    await pp._tick(bot, sync_db_path)
    await pp._tick(bot, sync_db_path)

    assert channel.send.await_count == 1


async def test_tick_stamps_even_when_the_nudge_fails_to_send(sync_db_path):
    """A room we can't post in would otherwise re-qualify on every tick."""
    bot, channel, base = _reminder_env(sync_db_path)
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))
    _say(sync_db_path, 1, 0, base=base)

    await pp._tick(bot, sync_db_path)

    assert _stamp_of(sync_db_path) == pytest.approx(time.time(), abs=30)


async def test_tick_holds_the_nudge_in_a_tick_that_posts_a_question(sync_db_path):
    """The auto-question already pings both members — one ping, not two."""
    bot, channel, base = _reminder_env(sync_db_path)
    _say(sync_db_path, 1, 0, base=base)
    with open_db(sync_db_path) as conn:
        pp._advance_next_question(conn, "s1", time.time() - 1)

    await pp._tick(bot, sync_db_path)

    assert channel.send.await_count == 1
    assert "question" in channel.send.await_args.args[0].lower()
    assert _stamp_of(sync_db_path) == 0  # still armed for the next tick


async def test_tick_holds_the_nudge_in_a_tick_that_warns_of_closing(sync_db_path):
    """The closing warning fires this very tick, so the row's flag is still
    unset in the loop's copy — the nudge has to notice anyway."""
    bot, channel, base = _reminder_env(sync_db_path)
    _say(sync_db_path, 1, 0, base=base)
    with open_db(sync_db_path) as conn:
        conn.execute(
            "UPDATE pen_pals_sessions SET expiry_at = ? WHERE session_id = 's1'",
            (time.time() + 600,),  # inside the 1h warn window
        )

    await pp._tick(bot, sync_db_path)

    assert channel.send.await_count == 1
    assert "closes in" in channel.send.await_args.args[0]
    assert _stamp_of(sync_db_path) == 0


async def test_tick_never_nudges_when_the_dial_is_off(sync_db_path):
    bot, channel, base = _reminder_env(sync_db_path, seconds=0)
    _say(sync_db_path, 1, 0, base=base)

    await pp._tick(bot, sync_db_path)

    channel.send.assert_not_awaited()


# ── the panel's failed-edit retry ────────────────────────────────────────────


async def test_retry_failed_panel_edits_reapplies_a_queued_guild():
    """``StickyPanel.refresh`` queues a guild when Discord rejects the edit and
    leaves the signature stale, expecting an owner with a loop to drain it. This
    cog calls ``refresh`` in two places and never drained the queue, so one 5xx
    left the panel stale until something else happened to move it — and the
    guild id piled up in a set nobody read (2026-08-06 review, F5).
    """
    cog = MagicMock()
    cog.panel.take_retries = MagicMock(return_value={GUILD_ID})
    cog.panel.refresh = AsyncMock()
    bot = MagicMock()
    bot.get_cog.return_value = cog

    await pp._retry_failed_panel_edits(bot)

    cog.panel.refresh.assert_awaited_once_with(GUILD_ID)


async def test_retry_failed_panel_edits_does_nothing_when_nothing_failed():
    cog = MagicMock()
    cog.panel.take_retries = MagicMock(return_value=set())
    cog.panel.refresh = AsyncMock()
    bot = MagicMock()
    bot.get_cog.return_value = cog

    await pp._retry_failed_panel_edits(bot)

    cog.panel.refresh.assert_not_awaited()


async def test_retry_failed_panel_edits_survives_one_guild_failing():
    """Two guilds queued, the first still broken: the second must still be
    retried rather than the loop tick aborting."""
    cog = MagicMock()
    cog.panel.take_retries = MagicMock(return_value={GUILD_ID, GUILD_ID + 1})
    cog.panel.refresh = AsyncMock(side_effect=[RuntimeError("boom"), None])
    bot = MagicMock()
    bot.get_cog.return_value = cog

    await pp._retry_failed_panel_edits(bot)

    assert cog.panel.refresh.await_count == 2


async def test_channel_delete_forgets_the_panel_when_its_own_channel_goes(
    sync_db_path,
):
    """The panel's ids used to outlive its channel, leaving the dashboard
    reporting a panel that could not exist (2026-08-06 review, F10). Runs before
    the session lookup so a deleted panel channel is cleared either way."""
    cog = pp.PenPalsCog(MagicMock(), SimpleNamespace(db_path=sync_db_path))
    cog.panel = MagicMock()
    cog.panel.on_channel_delete = AsyncMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242

    await cog._on_channel_delete(channel)

    cog.panel.on_channel_delete.assert_awaited_once_with(channel)


# ── Expiry: requeue, the inactivity gate, and the pool audit ──────────
#
# Until 2026-08-14 expiry was a dead end — it closed the session and told
# nobody anything. Since a match is the only thing that drains the pool, every
# round left it smaller, and TGM's ran down to one member and stalled for five
# days. These cover the refill and the one gate on it.


def _expiring_session(
    conn,
    session_id: str = "exp",
    *,
    user1_id: int = 1,
    user2_id: int = 2,
    channel_id: int = 4242,
    guild_id: int = GUILD_ID,
):
    """An active session whose 24 hours are already up."""
    pp._create_session(
        conn, session_id, guild_id, channel_id, user1_id, user2_id, time.time() - 90000
    )
    return pp._get_active_session(conn, guild_id, user1_id)


def _said_something(conn, user_id: int, *, channel_id: int = 4242, guild_id: int = GUILD_ID):
    """Log a message from *user_id* in their pen pal channel.

    The engagement test reads the guild-wide message log, the same source the
    reply reminder uses — Pen Pals stores no chat history of its own.
    """
    conn.execute(
        "INSERT INTO messages (guild_id, channel_id, author_id, ts) VALUES (?, ?, ?, ?)",
        (guild_id, channel_id, user_id, int(time.time())),
    )


def _pool_events(conn, guild_id: int = GUILD_ID) -> list[tuple[int, str, str]]:
    return [
        (r["user_id"], r["action"], r["reason"])
        for r in pp._recent_pool_events(conn, guild_id)
    ]


def test_expiry_returns_both_members_to_the_pool(sync_db_path):
    """The refill. Before this, expiry re-pooled nobody and the pool only ever
    shrank — the whole reason The Golden Meadow stopped matching."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        _said_something(conn, 2)

        requeued, skipped = pp._close_expired_and_requeue(conn, row)

        assert sorted(requeued) == [1, 2]
        assert skipped == []
        assert sorted(r["user_id"] for r in pp._get_pool(conn, GUILD_ID)) == [1, 2]


def test_expiry_leaves_out_a_member_who_never_spoke(sync_db_path):
    """The one gate on the refill: a member who ghosted is not recycled into
    someone else's 24 hours. Their partner, who showed up, still is."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)  # 2 never posted

        requeued, skipped = pp._close_expired_and_requeue(conn, row)

        assert requeued == [1]
        assert skipped == [2]
        assert [r["user_id"] for r in pp._get_pool(conn, GUILD_ID)] == [1]


def test_expiry_pools_nobody_when_the_chat_was_silent(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)

        requeued, skipped = pp._close_expired_and_requeue(conn, row)

        assert requeued == []
        assert sorted(skipped) == [1, 2]
        assert pp._get_pool(conn, GUILD_ID) == []


def test_expiry_reads_only_its_own_channel(sync_db_path):
    """Talking somewhere else in the server is not talking to your pen pal."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn, channel_id=4242)
        _said_something(conn, 1, channel_id=9999)  # a different channel

        requeued, skipped = pp._close_expired_and_requeue(conn, row)

        assert requeued == []
        assert sorted(skipped) == [1, 2]


def test_expiry_closes_the_session_as_expired(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        pp._close_expired_and_requeue(conn, row)

        stored = conn.execute(
            "SELECT state, close_reason FROM pen_pals_sessions WHERE session_id = 'exp'"
        ).fetchone()
        assert (stored["state"], stored["close_reason"]) == ("closed", "expired")


def test_expiry_loses_the_race_and_changes_nothing(sync_db_path):
    """Another handler already closed it: no second close, no re-pooling.

    This is the guard that keeps our own ``channel.delete`` from being handled
    twice — the expiry claims the close, and whatever the delete wakes up finds
    nothing left to do.
    """
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        _said_something(conn, 2)
        pp._claim_close(conn, "exp", "channel_deleted")

        assert pp._close_expired_and_requeue(conn, row) is None
        assert pp._get_pool(conn, GUILD_ID) == []
        assert _pool_events(conn) == []


def test_channel_delete_after_an_expiry_claim_repools_nobody(sync_db_path):
    """The race the expiry comment warns about, as a state test.

    Expiry claims the close, then deletes the channel; that delete fires
    ``on_guild_channel_delete``. If the listener could still see the session it
    would treat a chat that simply ran its course as abandoned — DMing both
    members "your partner is no longer available" and re-pooling them. Two
    independent guards stop it: the lookup filters on ``state = 'active'``, and
    ``_claim_close`` re-checks the same thing.
    """
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn, channel_id=4242)
        _said_something(conn, 1)
        _said_something(conn, 2)
        pp._close_expired_and_requeue(conn, row)
        before = _pool_events(conn)

        # What the listener does, in order.
        assert pp._get_session_by_channel(conn, 4242) is None
        assert pp._close_abnormal_and_requeue(conn, row, "channel_deleted", None) is None

        # Nothing moved, and nobody was told their partner vanished.
        assert _pool_events(conn) == before
        assert conn.execute(
            "SELECT close_reason FROM pen_pals_sessions WHERE session_id = 'exp'"
        ).fetchone()["close_reason"] == "expired"


def test_expiry_keeps_a_seat_a_member_already_has(sync_db_path):
    """Re-pooling someone mid-chat would hand them a second one."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        _said_something(conn, 2)
        pp._create_session(conn, "other", GUILD_ID, 5555, 2, 3, time.time())

        requeued, skipped = pp._close_expired_and_requeue(conn, row)

        assert requeued == [1]  # 2 is already chatting elsewhere
        assert skipped == []


def test_requeued_member_waits_out_the_cooldown_before_matching(sync_db_path):
    """Re-pooled the instant the chat ends, but not re-matched then.

    The pool is where they wait; ``match_cooldown_seconds`` is what paces them,
    measured from this close. Without that, auto-requeue would be a surprise
    match seconds after a goodbye.
    """
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 3600)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        _said_something(conn, 2)
        pp._close_expired_and_requeue(conn, row)

        now = time.time()
        assert pp._eligible_pool(conn, GUILD_ID, now, 3600) == []
        assert sorted(pp._eligible_pool(conn, GUILD_ID, now + 3601, 3600)) == [1, 2]


# ── Pool audit trail ──────────────────────────────────────────────────


def test_expiry_records_why_each_member_moved_or_did_not(sync_db_path):
    """'Did they drop out or get matched?' has to be answerable from the data —
    it wasn't, which is why a pool stuck at one member went unseen for days."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        pp._close_expired_and_requeue(conn, row)

        assert sorted(_pool_events(conn)) == [
            (1, pp.POOL_JOIN, "requeue_expired"),
            (2, pp.POOL_SKIP, "inactive"),
        ]


def test_abnormal_close_records_its_requeue(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        pp._close_abnormal_and_requeue(conn, row, "channel_deleted", None)

        assert sorted(_pool_events(conn)) == [
            (1, pp.POOL_JOIN, "requeue_abnormal"),
            (2, pp.POOL_JOIN, "requeue_abnormal"),
        ]


def test_abnormal_close_records_the_departed_member_leaving_the_pool(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        pp._add_to_pool(conn, GUILD_ID, 2)
        pp._close_abnormal_and_requeue(conn, row, "member_left", 2)

        assert (2, pp.POOL_LEAVE, "departed") in _pool_events(conn)


def test_recent_pool_events_are_newest_first_and_scoped_to_the_guild(sync_db_path):
    with open_db(sync_db_path) as conn:
        pp._record_pool_event(conn, GUILD_ID, 1, pp.POOL_JOIN, "panel", at=100.0)
        pp._record_pool_event(conn, GUILD_ID, 1, pp.POOL_LEAVE, "matched", at=200.0)
        pp._record_pool_event(conn, GUILD_ID + 1, 9, pp.POOL_JOIN, "panel", at=300.0)

        assert _pool_events(conn) == [
            (1, pp.POOL_LEAVE, "matched"),
            (1, pp.POOL_JOIN, "panel"),
        ]


async def test_tick_expiry_refills_the_pool_and_says_so(sync_db_path, monkeypatch):
    """End to end through the tick: the wave that ends is the wave that starts.

    The bug this closes — a normal expiry drained two members out of the pool
    and put nobody back, so the pool only ever shrank.
    """
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    monkeypatch.setattr("bot_modules.economy.game_rewards.fire_member_trigger", AsyncMock())
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._create_session(
            conn, "s1", GUILD_ID, 4242, 1, 2, time.time() - 10, session_seconds=0,
        )
        _said_something(conn, 1)
        _said_something(conn, 2)

    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.delete = AsyncMock(side_effect=_echo_channel_delete(bot, sync_db_path, 4242))
    bot.get_channel.return_value = channel

    await pp._tick(bot, sync_db_path)

    assert _pool_ids(sync_db_path) == [1, 2]
    for uid in (1, 2):
        send = guild.get_member(uid).send
        assert "back in the Pen Pals pool" in send.await_args.kwargs["embed"].description
        # One button, and it undoes what just happened to them.
        item = send.await_args.kwargs["view"].children[0]
        assert item.custom_id == f"pen_pals:dm:leave:{GUILD_ID}"


async def test_tick_expiry_offers_a_rejoin_button_to_the_member_left_out(
    sync_db_path, monkeypatch
):
    """Being unqueued has to be one tap to undo, or it's just a dead end."""
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    monkeypatch.setattr("bot_modules.economy.game_rewards.fire_member_trigger", AsyncMock())
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._create_session(
            conn, "s1", GUILD_ID, 4242, 1, 2, time.time() - 10, session_seconds=0,
        )
        _said_something(conn, 1)  # 2 stayed quiet

    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.delete = AsyncMock()
    bot.get_channel.return_value = channel

    await pp._tick(bot, sync_db_path)

    assert _pool_ids(sync_db_path) == [1]
    quiet = guild.get_member(2).send.await_args.kwargs
    assert "unqueued you" in quiet["embed"].description
    assert quiet["view"].children[0].custom_id == f"pen_pals:dm:join:{GUILD_ID}"


async def test_dm_join_button_still_enforces_the_opt_in_role(sync_db_path, monkeypatch):
    """A DM button must not be a way around the role gate the panel enforces."""
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    _configure(sync_db_path, opt_in_role_id=555)
    guild = FakeGuild(id=GUILD_ID)
    guild.roles[555] = FakeRole(id=555, name="Denizen")
    guild.members[7] = FakeMember(id=7)  # in the server, but without the role
    interaction = fake_interaction(user=FakeUser(id=7), guild=None)
    interaction.client = MagicMock(spec=discord.Client)
    interaction.client.get_guild.return_value = guild

    await pp._handle_join(interaction, sync_db_path, from_guild_id=GUILD_ID, source="dm")

    assert "Denizen" in interaction.response.send_message.await_args.args[0]
    assert _pool_ids(sync_db_path) == []


async def test_dm_join_button_records_the_dm_as_the_source(sync_db_path, monkeypatch):
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    _configure(sync_db_path)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[7] = FakeMember(id=7)
    interaction = fake_interaction(user=FakeUser(id=7), guild=None)
    interaction.client = MagicMock(spec=discord.Client)
    interaction.client.get_guild.return_value = guild

    await pp._handle_join(interaction, sync_db_path, from_guild_id=GUILD_ID, source="dm")

    assert _pool_ids(sync_db_path) == [7]
    with open_db(sync_db_path) as conn:
        assert _pool_events(conn) == [(7, pp.POOL_JOIN, "dm")]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        pytest.param({}, "panel", id="panel"),
        pytest.param({"source": "command"}, "command", id="command"),
    ],
)
async def test_join_records_where_it_came_from(sync_db_path, monkeypatch, kwargs, reason):
    """The audit is only useful if it distinguishes the paths in."""
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    _configure(sync_db_path)
    interaction = _join_interaction(7)

    await pp._handle_join(interaction, sync_db_path, **kwargs)

    with open_db(sync_db_path) as conn:
        assert _pool_events(conn) == [(7, pp.POOL_JOIN, reason)]


async def test_leaving_the_pool_is_recorded(sync_db_path, monkeypatch):
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 7)

    await pp._handle_leave(_join_interaction(7), sync_db_path)

    with open_db(sync_db_path) as conn:
        assert _pool_events(conn) == [(7, pp.POOL_LEAVE, "panel")]


async def test_matching_records_both_members_leaving_the_pool(sync_db_path, pair_env):
    """Otherwise a pool that drained looks the same whether they were matched
    or gave up — the exact ambiguity that hid the stall."""
    bot, _channel, _created = pair_env
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)

    assert await pp._do_pair(bot, sync_db_path, GUILD_ID, 1, 2)

    with open_db(sync_db_path) as conn:
        assert sorted(_pool_events(conn)) == [
            (1, pp.POOL_LEAVE, "matched"),
            (2, pp.POOL_LEAVE, "matched"),
        ]


# ── Expiry fixes from the 2026-08-15 review ───────────────────────────


def test_expiry_does_not_unqueue_a_member_who_still_has_a_seat(sync_db_path):
    """The seat check has to beat the engagement check.

    A member already pooled or already in another chat keeps what they have
    either way. Running the silence test on them first told a still-queued
    member "we've unqueued you" and handed them a Join button that answers
    "you're already in the pool" (review finding 3).
    """
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        pp._add_to_pool(conn, GUILD_ID, 2)  # already queued, and never spoke

        requeued, skipped = pp._close_expired_and_requeue(conn, row)

        assert requeued == [1]
        assert skipped == []  # not "unqueued" — they were never unqueued
        assert (2, pp.POOL_SKIP, "inactive") not in _pool_events(conn)


async def test_dm_join_button_refuses_someone_who_left_the_server(sync_db_path, monkeypatch):
    """A DM outlives the membership it was sent to.

    A panel click proves membership; a DM button proves nothing. Without this,
    a member skipped for inactivity who then left could tap the old button and
    insert a pool row for a guild they aren't in — and nothing clears it, so
    `_do_round` burns a real member's match on them every round (review
    finding 2).
    """
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    _configure(sync_db_path)  # no opt-in role — the gate that used to be the
    guild = FakeGuild(id=GUILD_ID)  # only thing consulting get_member
    interaction = fake_interaction(user=FakeUser(id=7), guild=None)
    interaction.client = MagicMock(spec=discord.Client)
    interaction.client.get_guild.return_value = guild

    await pp._handle_join(interaction, sync_db_path, from_guild_id=GUILD_ID, source="dm")

    assert "not in that server" in interaction.response.send_message.await_args.args[0]
    assert _pool_ids(sync_db_path) == []


async def test_expiry_dm_states_the_wait_instead_of_promising_a_match(
    sync_db_path, monkeypatch
):
    """`match_cooldown_seconds` ships at 30 days, so "a new match can come along
    any time" would be a month wrong on an untouched guild (review finding 1).
    Nothing in the copy names a duration the config can change underneath it."""
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    monkeypatch.setattr("bot_modules.economy.game_rewards.fire_member_trigger", AsyncMock())
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 172800)
    with open_db(sync_db_path) as conn:
        pp._create_session(
            conn, "s1", GUILD_ID, 4242, 1, 2, time.time() - 10, session_seconds=0,
        )
        _said_something(conn, 1)
        _said_something(conn, 2)

    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.delete = AsyncMock()
    bot.get_channel.return_value = channel

    await pp._tick(bot, sync_db_path)

    body = guild.get_member(1).send.await_args.kwargs["embed"].description
    assert "any time" not in body
    assert "24 hours" not in body          # session length is configurable
    assert "matched again <t:" in body     # the wait, as a live timestamp


async def test_expiry_dm_promises_an_immediate_match_only_without_a_cooldown(
    sync_db_path, monkeypatch
):
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    monkeypatch.setattr("bot_modules.economy.game_rewards.fire_member_trigger", AsyncMock())
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    with open_db(sync_db_path) as conn:
        pp._create_session(
            conn, "s1", GUILD_ID, 4242, 1, 2, time.time() - 10, session_seconds=0,
        )
        _said_something(conn, 1)
        _said_something(conn, 2)

    guild = _make_guild_mock(1, 2)
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.delete = AsyncMock()
    bot.get_channel.return_value = channel

    await pp._tick(bot, sync_db_path)

    body = guild.get_member(1).send.await_args.kwargs["embed"].description
    assert "any time" in body


async def test_unpairable_pool_stops_repeating_itself_in_the_log(sync_db_path, monkeypatch, caplog):
    """Two ex-partners alone in the pool can never be paired, so the sweep runs
    every tick forever. It should say "0 pairs" once, not every five minutes
    (review finding 5)."""
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    monkeypatch.setattr(pp, "_do_round", AsyncMock(return_value=(0, 2)))
    monkeypatch.setattr(pp, "_LAST_SWEEP_LOG", {})
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)

    with caplog.at_level("INFO", logger="dungeonkeeper.pen_pals"):
        await pp._tick(MagicMock(), sync_db_path)
        await pp._tick(MagicMock(), sync_db_path)
        await pp._tick(MagicMock(), sync_db_path)

    assert sum("swept guild" in r.message for r in caplog.records) == 1


async def test_expiry_does_not_repool_a_member_who_left_the_guild(sync_db_path):
    """`on_member_remove` is the only thing that prunes the pool, and a member
    who leaves while the bot is down never fires it — their session survives to
    expiry. Re-pooling them plants a row nothing can clear, and `_do_round`
    burns a real member's match on it every round (review finding 1)."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        _said_something(conn, 2)

        requeued, skipped = pp._close_expired_and_requeue(conn, row, present={1})

        assert requeued == [1]
        assert skipped == []  # nobody left to tell, and no button that helps
        assert [r["user_id"] for r in pp._get_pool(conn, GUILD_ID)] == [1]
        assert (2, pp.POOL_SKIP, "departed") in _pool_events(conn)


async def test_tick_reads_membership_off_the_channel_it_already_resolved(
    sync_db_path, monkeypatch
):
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    monkeypatch.setattr("bot_modules.economy.game_rewards.fire_member_trigger", AsyncMock())
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._create_session(
            conn, "s1", GUILD_ID, 4242, 1, 2, time.time() - 10, session_seconds=0,
        )
        _said_something(conn, 1)
        _said_something(conn, 2)

    guild = _make_guild_mock(1)  # 2 is gone from the guild
    bot = _make_bot_mock(guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.guild = guild
    channel.delete = AsyncMock()
    bot.get_channel.return_value = channel

    await pp._tick(bot, sync_db_path)

    assert _pool_ids(sync_db_path) == [1]


async def test_a_round_that_pairs_someone_always_logs(sync_db_path, monkeypatch, caplog):
    """Pairing is an event, not a state. De-duping it would hide every real
    match on a guild that steadily pairs the same count (review finding 3)."""
    _configure(sync_db_path)
    _set_cooldown(sync_db_path, 0)
    monkeypatch.setattr(pp, "_do_round", AsyncMock(return_value=(1, 0)))
    monkeypatch.setattr(pp, "_LAST_SWEEP_LOG", {})
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)

    with caplog.at_level("INFO", logger="dungeonkeeper.pen_pals"):
        await pp._tick(MagicMock(), sync_db_path)
        await pp._tick(MagicMock(), sync_db_path)
        await pp._tick(MagicMock(), sync_db_path)

    assert sum("swept guild" in r.message for r in caplog.records) == 3


# ── Durable opt-out ───────────────────────────────────────────────────
#
# Leaving the pool used to hold only until your current chat ended: the pool
# is current-state only, so `_handle_leave` deleted a row and nothing recorded
# that you wanted to stay out. Since expiry started re-pooling both members
# (2026-08-15) that made leaving nearly meaningless — a TGM member was matched
# on 08-16 and 08-19 having never once been put in the pool by her own hand.
# The flag is the durable "don't match me", and these are its enforcement.


def test_expiry_does_not_repool_an_opted_out_member(sync_db_path):
    """The defect, at the layer it lives on. She spoke in the chat, so the
    engagement gate passes and the old code re-pools her regardless."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)
        _said_something(conn, 2)
        pp._set_opt_out(conn, GUILD_ID, 2)

        requeued, skipped = pp._close_expired_and_requeue(conn, row)

        assert requeued == [1]
        # Not "skipped" either: skipped members are DM'd "that one stayed
        # quiet" with a Join button, which is the wrong thing to send someone
        # who just asked to be left alone.
        assert skipped == []
        assert [r["user_id"] for r in pp._get_pool(conn, GUILD_ID)] == [1]
        assert (2, pp.POOL_SKIP, "opted_out") in _pool_events(conn)


def test_abnormal_close_does_not_repool_an_opted_out_member(sync_db_path):
    """The other requeue path. A partner leaving the server is not consent to
    be matched again."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, time.time())
        row = pp._get_active_session(conn, GUILD_ID, 1)
        pp._set_opt_out(conn, GUILD_ID, 2)

        requeued = pp._close_abnormal_and_requeue(
            conn, row, "channel_deleted", departed_user_id=None
        )

        assert requeued == [1]
        assert [r["user_id"] for r in pp._get_pool(conn, GUILD_ID)] == [1]
        assert (2, pp.POOL_SKIP, "opted_out") in _pool_events(conn)


def test_opt_out_beats_the_engagement_gate(sync_db_path):
    """A quiet member who also opted out is recorded as opted out, not
    inactive: 'skipped' earns them the "hop back in" DM and a Join button,
    which is the bot arguing with a decision they already made."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        row = _expiring_session(conn)
        _said_something(conn, 1)  # 2 never posted *and* opted out
        pp._set_opt_out(conn, GUILD_ID, 2)

        requeued, skipped = pp._close_expired_and_requeue(conn, row)

        assert requeued == [1]
        assert skipped == []
        assert (2, pp.POOL_SKIP, "opted_out") in _pool_events(conn)
        assert (2, pp.POOL_SKIP, "inactive") not in _pool_events(conn)


def test_opt_out_is_recorded_once(sync_db_path):
    """Pressing Leave twice doesn't move the timestamp — /penpals status
    reports when they first asked, not when they last pressed a button."""
    with open_db(sync_db_path) as conn:
        pp._set_opt_out(conn, GUILD_ID, 1, at=1000.0)
        pp._set_opt_out(conn, GUILD_ID, 1, at=2000.0)

        assert pp._opted_out_at(conn, GUILD_ID, 1) == 1000.0
        assert [r["user_id"] for r in pp._get_opt_outs(conn, GUILD_ID)] == [1]


def test_opt_out_is_per_guild(sync_db_path):
    """Two guilds run their own pools; leaving one is not leaving the other."""
    with open_db(sync_db_path) as conn:
        pp._set_opt_out(conn, GUILD_ID, 1)

        assert pp._is_opted_out(conn, GUILD_ID, 1)
        assert not pp._is_opted_out(conn, 12345, 1)


async def test_leave_while_matched_opts_out_without_ending_the_chat(sync_db_path):
    """The heart of the fix. Every leave surface used to answer "❌ You're not
    in the pool" to a matched member — who is never in the pool — so the only
    window to opt out was the gap between chats, reachable only through a DM
    that a member with DMs closed never sees."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, time.time())

    interaction = _join_interaction(1)
    await pp._handle_leave(interaction, sync_db_path)

    msg = interaction.response.send_message.await_args.args[0]
    assert "❌" not in msg
    assert "won't be put back in the pool" in msg
    with open_db(sync_db_path) as conn:
        assert pp._is_opted_out(conn, GUILD_ID, 1)
        # Leaving the pool is not ending a conversation.
        assert pp._get_active_session(conn, GUILD_ID, 1) is not None


async def test_leave_from_the_pool_opts_out_too(sync_db_path, monkeypatch):
    """The pool row still goes — but so does the member's place in every
    future round, which is what "Leave Pool" always looked like it meant."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())

    await pp._handle_leave(_join_interaction(1), sync_db_path)

    assert _pool_ids(sync_db_path) == []
    with open_db(sync_db_path) as conn:
        assert pp._is_opted_out(conn, GUILD_ID, 1)
        assert (1, pp.POOL_LEAVE, "panel") in _pool_events(conn)


async def test_joining_clears_the_opt_out(sync_db_path, monkeypatch):
    _configure(sync_db_path)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    with open_db(sync_db_path) as conn:
        pp._set_opt_out(conn, GUILD_ID, 1)

    await pp._handle_join(_join_interaction(1), sync_db_path)

    with open_db(sync_db_path) as conn:
        assert not pp._is_opted_out(conn, GUILD_ID, 1)
    assert _pool_ids(sync_db_path) == [1]


async def test_joining_mid_chat_clears_the_opt_out_and_says_so(sync_db_path, monkeypatch):
    """Changing your mind while still matched. The old "❌ you already have a
    pen pal" would report the click as a no-op when it in fact decided what
    happens when that chat closes."""
    _configure(sync_db_path)
    monkeypatch.setattr(pp, "_refresh_panel", AsyncMock())
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, time.time())
        pp._set_opt_out(conn, GUILD_ID, 1)

    interaction = _join_interaction(1)
    await pp._handle_join(interaction, sync_db_path)

    msg = interaction.response.send_message.await_args.args[0]
    assert "back in Pen Pals" in msg
    with open_db(sync_db_path) as conn:
        assert not pp._is_opted_out(conn, GUILD_ID, 1)
    # Still no second seat: clearing the flag is not joining the pool.
    assert _pool_ids(sync_db_path) == []


async def test_join_blocked_by_the_role_gate_leaves_the_opt_out_alone(sync_db_path):
    """The gates run before the flag is touched, so a refused join can't
    quietly un-pause someone who isn't allowed in anyway."""
    _configure(sync_db_path, opt_in_role_id=555)
    g = FakeGuild(id=GUILD_ID)
    g.roles[555] = FakeRole(id=555, name="Verified")
    with open_db(sync_db_path) as conn:
        pp._set_opt_out(conn, GUILD_ID, 1)

    await pp._handle_join(_join_interaction(1, guild=g), sync_db_path)

    with open_db(sync_db_path) as conn:
        assert pp._is_opted_out(conn, GUILD_ID, 1)


def test_eligible_pool_skips_an_opted_out_member(sync_db_path):
    """Last gate. Nothing should pool an opted-out member, but a preference
    the bot holds and doesn't honour is worse than none — so a row arriving
    from anywhere else (backfill script, a restored backup) still can't be
    handed a partner."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._add_to_pool(conn, GUILD_ID, 1, joined_at=100.0)
        pp._add_to_pool(conn, GUILD_ID, 2, joined_at=200.0)
        pp._set_opt_out(conn, GUILD_ID, 2)

        assert pp._eligible_pool(conn, GUILD_ID, time.time(), 0) == [1]


async def test_status_reports_the_paused_state(sync_db_path):
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._set_opt_out(conn, GUILD_ID, 1, at=1000.0)

    interaction = _join_interaction(1)
    cog = pp.PenPalsCog(MagicMock(), MagicMock(db_path=sync_db_path))
    await cog.penpals_status.callback(cog, interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "left the Pen Pals pool" in msg
    assert "<t:1000:R>" in msg


async def test_status_tells_a_matched_member_they_are_paused(sync_db_path):
    """Opting out mid-chat is invisible to your partner by design, so status
    is the only place it's ever shown back to you."""
    _configure(sync_db_path)
    with open_db(sync_db_path) as conn:
        pp._create_session(conn, "s1", GUILD_ID, 4242, 1, 2, time.time())
        pp._set_opt_out(conn, GUILD_ID, 1)

    interaction = _join_interaction(1)
    cog = pp.PenPalsCog(MagicMock(), MagicMock(db_path=sync_db_path))
    await cog.penpals_status.callback(cog, interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "won't be matched again when this chat closes" in msg
