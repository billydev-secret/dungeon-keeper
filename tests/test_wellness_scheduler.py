"""Tests for bot_modules.services.wellness_scheduler.

The scheduler is 280 statements at 0% coverage before this file. Strategy:

- Pure helpers (_format_minute, _badge_rank, _iso_week_for) are unit-tested directly.
- Embed builders are smoke-tested for shape / fields.
- DB-driven coroutines (_lift_expired_slow_mode, _resume_expired_pauses, etc.)
  are driven against a migrated sqlite DB seeded via the wellness_service helpers.
- Discord-bound functions (_try_dm, _send_blackout_entry_dm, _process_blackout_transitions)
  use small dataclass-shaped fakes / unittest.mock.AsyncMock.
- The wellness AI hook is monkeypatched to return a deterministic string so
  weekly-report tests stay offline.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from freezegun import freeze_time

from bot_modules.core.db_utils import open_db
from bot_modules.services import wellness_scheduler as scheduler
from bot_modules.services.wellness_scheduler import (
    _badge_rank,
    _build_active_embed,
    _build_weekly_report_embed,
    _credit_clean_days,
    _format_minute,
    _generate_and_send_weekly_report,
    _iso_week_for,
    _lift_expired_slow_mode,
    _nightly_maintenance,
    _post_milestone_celebrations,
    _process_blackout_transitions,
    _rebuild_active_list_for_guild,
    _resume_expired_pauses,
    _send_blackout_entry_dm,
    _try_dm,
    wellness_active_list_loop,
    wellness_tick_loop,
    wellness_weekly_report_loop,
)
from bot_modules.services.wellness_service import (
    WellnessBlackout,
    WellnessStreak,
    add_blackout,
    arm_slow_mode,
    ensure_streak,
    increment_streak_day,
    mark_blackout_active,
    opt_in_user,
    pause_user,
    upsert_wellness_config,
    user_now,
)
from migrations import apply_migrations_sync


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A migrated SQLite database file path."""
    path = tmp_path / "wellness.db"
    apply_migrations_sync(path)
    return path


# ── Minimal Discord-like fakes ───────────────────────────────────────


class _FakePermissions:
    def __init__(self, send_messages: bool = True, manage_messages: bool = True) -> None:
        self.send_messages = send_messages
        self.manage_messages = manage_messages


class _FakeMember:
    def __init__(self, user_id: int, display_name: str = "Member") -> None:
        self.id = user_id
        self.display_name = display_name
        self.mention = f"<@{user_id}>"
        self.send = AsyncMock()


class _FakeMessage:
    def __init__(self, message_id: int = 555) -> None:
        self.id = message_id
        self.edit = AsyncMock()
        self.pin = AsyncMock()


class _FakeTextChannel:
    """Stands in for discord.TextChannel; passes isinstance via spec arg below."""

    def __init__(self, channel_id: int, *, perms: _FakePermissions | None = None) -> None:
        self.id = channel_id
        self._perms = perms or _FakePermissions()
        self.send = AsyncMock(return_value=_FakeMessage(message_id=777))
        self.fetch_message = AsyncMock(return_value=_FakeMessage(message_id=888))

    def permissions_for(self, _me) -> _FakePermissions:
        return self._perms


class _FakeGuild:
    def __init__(self, guild_id: int = 10, members: dict | None = None) -> None:
        self.id = guild_id
        self.name = f"Guild{guild_id}"
        self.members = members or {}
        self.channels: dict = {}
        self.me = MagicMock()  # truthy

    def get_member(self, uid: int):
        return self.members.get(uid)

    def get_channel(self, cid: int):
        return self.channels.get(cid)


class _FakeBot:
    def __init__(self, guilds: list[_FakeGuild] | None = None) -> None:
        self.guilds = guilds or []


# Patch isinstance(channel, discord.TextChannel) checks in scheduler module.
@pytest.fixture(autouse=True)
def _patch_textchannel_isinstance(monkeypatch):
    """Allow our _FakeTextChannel to satisfy discord.TextChannel isinstance checks."""
    real_isinstance = isinstance

    def fake_isinstance(obj, cls):
        if cls is discord.TextChannel and isinstance(obj, _FakeTextChannel):
            return True
        return real_isinstance(obj, cls)

    monkeypatch.setattr(scheduler, "isinstance", fake_isinstance, raising=False)


# ── Pure helpers ─────────────────────────────────────────────────────


def test_format_minute_midnight():
    assert _format_minute(0) == "00:00"


def test_format_minute_noon():
    assert _format_minute(12 * 60) == "12:00"


def test_format_minute_23_30():
    assert _format_minute(23 * 60 + 30) == "23:30"


def test_format_minute_wraps_with_zero_padding():
    assert _format_minute(7 * 60 + 5) == "07:05"


def test_badge_rank_recognizes_seed():
    assert _badge_rank("🌱") == 0


def test_badge_rank_recognizes_top_tier():
    assert _badge_rank("👑") == 4


def test_badge_rank_unknown_returns_minus_one():
    assert _badge_rank("🥨") == -1


def test_iso_week_for_monday():
    now_local = datetime(2026, 5, 25, 9, 0)  # Monday
    iso_year, iso_week, week_start_iso = _iso_week_for(now_local)
    assert iso_year == 2026
    assert iso_week == 22
    assert week_start_iso == "2026-05-25"


def test_iso_week_for_sunday_returns_monday_of_that_iso_week():
    now_local = datetime(2026, 5, 31, 10, 0)  # Sunday
    iso_year, iso_week, week_start_iso = _iso_week_for(now_local)
    assert iso_year == 2026
    assert iso_week == 22
    assert week_start_iso == "2026-05-25"  # Monday of the same ISO week


# ── Embed builders ───────────────────────────────────────────────────


def test_build_active_embed_empty_returns_seed_message():
    guild = _FakeGuild()
    embed = _build_active_embed(guild, [])  # type: ignore[arg-type]
    assert isinstance(embed, discord.Embed)
    assert embed.description is not None
    assert "No one has opted in" in embed.description


def test_build_active_embed_lists_entries():
    guild = _FakeGuild(members={7: _FakeMember(7, display_name="Alice")})
    streak = WellnessStreak(
        guild_id=10,
        user_id=7,
        current_days=5,
        personal_best=5,
        streak_start_date="2026-05-26",
        last_violation_date=None,
        current_badge="🌱",
        celebrated_badge="🌱",
        updated_at=0.0,
    )
    embed = _build_active_embed(guild, [(7, streak)])  # type: ignore[arg-type]
    assert embed.description is not None
    assert "Alice" in embed.description
    assert "5 days" in embed.description


def test_build_active_embed_singular_day():
    guild = _FakeGuild(members={7: _FakeMember(7, display_name="Solo")})
    streak = WellnessStreak(
        guild_id=10,
        user_id=7,
        current_days=1,
        personal_best=1,
        streak_start_date=None,
        last_violation_date=None,
        current_badge="🌱",
        celebrated_badge="🌱",
        updated_at=0.0,
    )
    embed = _build_active_embed(guild, [(7, streak)])  # type: ignore[arg-type]
    assert embed.description is not None
    assert "1 day" in embed.description
    assert "1 days" not in embed.description


def test_build_active_embed_falls_back_to_user_id_when_member_missing():
    guild = _FakeGuild()
    streak = WellnessStreak(
        guild_id=10,
        user_id=42,
        current_days=3,
        personal_best=3,
        streak_start_date=None,
        last_violation_date=None,
        current_badge="🌟",
        celebrated_badge="🌟",
        updated_at=0.0,
    )
    embed = _build_active_embed(guild, [(42, streak)])  # type: ignore[arg-type]
    assert embed.description is not None
    assert "User 42" in embed.description


def test_build_active_embed_truncates_to_max_entries():
    guild = _FakeGuild()
    # Build 30 entries — _ACTIVE_MAX_ENTRIES is 25
    entries: list[tuple[int, WellnessStreak]] = []
    for i in range(30):
        s = WellnessStreak(
            guild_id=10,
            user_id=i,
            current_days=i,
            personal_best=i,
            streak_start_date=None,
            last_violation_date=None,
            current_badge="🌱",
            celebrated_badge="🌱",
            updated_at=0.0,
        )
        entries.append((i, s))
    embed = _build_active_embed(guild, entries)  # type: ignore[arg-type]
    assert embed.description is not None
    assert "and 5 more" in embed.description


def test_build_weekly_report_embed_shape():
    summary = {
        "badge": "🌟",
        "week_start": "2026-05-25",
        "week_end": "2026-05-31",
        "clean_days": 5,
        "compliance_pct": 71,
        "current_days": 12,
        "personal_best": 12,
        "is_personal_best": True,
    }
    embed = _build_weekly_report_embed("Alice", summary, "Keep going!")
    assert embed.title is not None and "Alice" in embed.title
    assert embed.description is not None
    assert "12 days" in embed.description
    assert "personal best" in embed.description
    assert "5/7" in embed.description
    assert "Keep going!" in embed.description


def test_build_weekly_report_embed_handles_missing_fields():
    """Missing optional keys still produce a coherent embed."""
    embed = _build_weekly_report_embed("Bob", {}, "hi")
    assert embed.description is not None
    # Should fall back to seed badge
    assert "🌱" in embed.description


# ── _try_dm ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_dm_success_returns_true():
    user = MagicMock()
    user.send = AsyncMock()
    ok = await _try_dm(user, content="hello")
    assert ok is True
    user.send.assert_awaited_once_with(content="hello")


@pytest.mark.asyncio
async def test_try_dm_forbidden_returns_false():
    user = MagicMock()
    user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    ok = await _try_dm(user, content="hi")
    assert ok is False


@pytest.mark.asyncio
async def test_try_dm_http_error_returns_false():
    user = MagicMock()
    user.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom")
    )
    ok = await _try_dm(user, embed=discord.Embed(title="x"))
    assert ok is False


@pytest.mark.asyncio
async def test_try_dm_passes_embed_kwarg():
    user = MagicMock()
    user.send = AsyncMock()
    embed = discord.Embed(title="z")
    ok = await _try_dm(user, embed=embed)
    assert ok is True
    user.send.assert_awaited_once_with(embed=embed)


# ── _send_blackout_entry_dm ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_blackout_entry_dm_sends_embed():
    member = MagicMock()
    member.send = AsyncMock()
    blackout = WellnessBlackout(
        id=1,
        guild_id=10,
        user_id=7,
        name="Night Owl",
        start_minute=23 * 60,
        end_minute=7 * 60,
        days_mask=127,
        enabled=True,
        created_at=0.0,
    )
    await _send_blackout_entry_dm(member, blackout)
    member.send.assert_awaited_once()
    embed = member.send.call_args.kwargs.get("embed")
    assert isinstance(embed, discord.Embed)
    assert embed.title is not None and "Night Owl" in embed.title
    assert embed.description is not None and "07:00" in embed.description


# ── _lift_expired_slow_mode ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_lift_expired_slow_mode_removes_expired_rows(db_path: Path):
    now = time.time()
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        arm_slow_mode(
            conn,
            10,
            7,
            triggered_by_cap_id=0,
            triggered_window_start=int(now) - 3600,
            active_until_ts=now - 60,  # already expired
        )
        opt_in_user(conn, 10, 8, timezone="UTC")
        arm_slow_mode(
            conn,
            10,
            8,
            triggered_by_cap_id=0,
            triggered_window_start=int(now),
            active_until_ts=now + 3600,  # still active
        )

    await _lift_expired_slow_mode(db_path)

    with open_db(db_path) as conn:
        remaining = conn.execute(
            "SELECT user_id FROM wellness_slow_mode WHERE guild_id = 10"
        ).fetchall()
    user_ids = {int(r["user_id"]) for r in remaining}
    assert 7 not in user_ids
    assert 8 in user_ids


# ── _resume_expired_pauses ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_expired_pauses_clears_old_paused_until(db_path: Path):
    now = time.time()
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        pause_user(conn, 10, 7, until=now - 60)  # already lapsed
        opt_in_user(conn, 10, 8, timezone="UTC")
        pause_user(conn, 10, 8, until=now + 3600)  # still paused

    await _resume_expired_pauses(db_path)

    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT user_id, paused_until FROM wellness_users WHERE guild_id = 10"
        ).fetchall()
    by_uid = {int(r["user_id"]): r["paused_until"] for r in rows}
    assert by_uid[7] is None
    assert by_uid[8] is not None


# ── _nightly_maintenance ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nightly_maintenance_runs_without_error(db_path: Path):
    """GC helpers no-op on an empty DB — exercise the wrapper."""
    await _nightly_maintenance(db_path)
    # And again with a single opted-in user, just to exercise the helpers
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
    await _nightly_maintenance(db_path)


# ── _credit_clean_days ───────────────────────────────────────────────


@freeze_time("2026-05-31 12:00:00")
@pytest.mark.asyncio
async def test_credit_clean_days_adds_history_row(db_path: Path):
    """Active user past reset hour with no row yet today gets a streak credit."""
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")  # daily_reset_hour defaults to 0
        ensure_streak(conn, 10, 7, "2026-05-30")

    guild = _FakeGuild(guild_id=10)
    bot = _FakeBot(guilds=[guild])
    await _credit_clean_days(bot, db_path)  # type: ignore[arg-type]

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT day FROM wellness_streak_history "
            "WHERE guild_id = 10 AND user_id = 7 AND day = '2026-05-31'"
        ).fetchone()
        streak = conn.execute(
            "SELECT current_days FROM wellness_streaks "
            "WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()
    assert row is not None
    assert int(streak["current_days"]) == 1


@freeze_time("2026-05-31 12:00:00")
@pytest.mark.asyncio
async def test_credit_clean_days_skips_users_violated_today(db_path: Path):
    """Users with last_violation_date == today must not get a credit."""
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-30")
        conn.execute(
            "UPDATE wellness_streaks SET last_violation_date = ? "
            "WHERE guild_id = 10 AND user_id = 7",
            ("2026-05-31",),
        )

    guild = _FakeGuild(guild_id=10)
    bot = _FakeBot(guilds=[guild])
    await _credit_clean_days(bot, db_path)  # type: ignore[arg-type]

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT day FROM wellness_streak_history "
            "WHERE guild_id = 10 AND user_id = 7 AND day = '2026-05-31'"
        ).fetchone()
    assert row is None


@freeze_time("2026-05-31 12:00:00")
@pytest.mark.asyncio
async def test_credit_clean_days_skips_already_credited_day(db_path: Path):
    """Idempotency: re-running on the same day produces no second history row."""
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-30")
        increment_streak_day(conn, 10, 7, "2026-05-31")

    guild = _FakeGuild(guild_id=10)
    bot = _FakeBot(guilds=[guild])
    await _credit_clean_days(bot, db_path)  # type: ignore[arg-type]

    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT day FROM wellness_streak_history "
            "WHERE guild_id = 10 AND user_id = 7"
        ).fetchall()
    assert len(rows) == 1


@freeze_time("2026-05-31 12:00:00")
@pytest.mark.asyncio
async def test_credit_clean_days_skips_paused_users(db_path: Path):
    """Paused users should never be credited."""
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        # Pause via direct insert because time is frozen
        conn.execute(
            "UPDATE wellness_users SET paused_until = ? WHERE guild_id = 10 AND user_id = 7",
            (time.time() + 3600,),
        )

    guild = _FakeGuild(guild_id=10)
    bot = _FakeBot(guilds=[guild])
    await _credit_clean_days(bot, db_path)  # type: ignore[arg-type]

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT day FROM wellness_streak_history WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()
    assert row is None


@freeze_time("2026-05-31 03:00:00")
@pytest.mark.asyncio
async def test_credit_clean_days_waits_for_reset_hour(db_path: Path):
    """Users whose daily_reset_hour hasn't been crossed yet are not credited."""
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        conn.execute(
            "UPDATE wellness_users SET daily_reset_hour = 6 "
            "WHERE guild_id = 10 AND user_id = 7"
        )

    guild = _FakeGuild(guild_id=10)
    bot = _FakeBot(guilds=[guild])
    await _credit_clean_days(bot, db_path)  # type: ignore[arg-type]

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT day FROM wellness_streak_history WHERE guild_id = 10"
        ).fetchone()
    assert row is None


# ── _process_blackout_transitions ────────────────────────────────────


@freeze_time("2026-05-31 23:30:00")
@pytest.mark.asyncio
async def test_process_blackout_transitions_marks_active_and_dms(db_path: Path):
    """Entering an active blackout marks the row and DMs the member."""
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        # Active right now (23:30 falls in 23:00-07:00 window) on all days
        add_blackout(
            conn, 10, 7,
            name="Night",
            start_minute=23 * 60,
            end_minute=7 * 60,
            days_mask=127,
        )

    member = _FakeMember(7)
    guild = _FakeGuild(guild_id=10, members={7: member})
    bot = _FakeBot(guilds=[guild])

    await _process_blackout_transitions(bot, db_path)  # type: ignore[arg-type]

    # Drain the asyncio.create_task DM
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT blackout_id FROM wellness_blackout_active WHERE guild_id = 10 AND user_id = 7"
        ).fetchall()
    assert len(rows) == 1
    member.send.assert_awaited()  # DM was dispatched


@freeze_time("2026-05-31 12:00:00")
@pytest.mark.asyncio
async def test_process_blackout_transitions_clears_inactive_marker(db_path: Path):
    """A blackout marked active but now outside its window is cleared."""
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        bid = add_blackout(
            conn, 10, 7,
            name="Night",
            start_minute=23 * 60,
            end_minute=7 * 60,
            days_mask=127,
        )
        # Pretend it was active before — but now it's noon, outside the window
        mark_blackout_active(conn, 10, 7, bid)

    guild = _FakeGuild(guild_id=10, members={7: _FakeMember(7)})
    bot = _FakeBot(guilds=[guild])

    await _process_blackout_transitions(bot, db_path)  # type: ignore[arg-type]

    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT blackout_id FROM wellness_blackout_active WHERE guild_id = 10"
        ).fetchall()
    assert rows == []


@freeze_time("2026-05-31 23:30:00")
@pytest.mark.asyncio
async def test_process_blackout_transitions_skips_paused_users(db_path: Path):
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        conn.execute(
            "UPDATE wellness_users SET paused_until = ? WHERE guild_id = 10 AND user_id = 7",
            (time.time() + 3600,),
        )
        add_blackout(
            conn, 10, 7,
            name="Night",
            start_minute=23 * 60,
            end_minute=7 * 60,
            days_mask=127,
        )

    guild = _FakeGuild(guild_id=10, members={7: _FakeMember(7)})
    bot = _FakeBot(guilds=[guild])
    await _process_blackout_transitions(bot, db_path)  # type: ignore[arg-type]

    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT blackout_id FROM wellness_blackout_active WHERE guild_id = 10"
        ).fetchall()
    assert rows == []


# ── _rebuild_active_list_for_guild ───────────────────────────────────


@pytest.mark.asyncio
async def test_rebuild_active_list_no_config_returns_early(db_path: Path):
    """When no wellness_config row exists for the guild, nothing happens."""
    guild = _FakeGuild(guild_id=10)
    await _rebuild_active_list_for_guild(db_path, guild)  # type: ignore[arg-type]
    # Smoke: no exception. Channel/send never touched.


@pytest.mark.asyncio
async def test_rebuild_active_list_no_channel_returns_early(db_path: Path):
    """Config without a channel_id is also skipped."""
    with open_db(db_path) as conn:
        upsert_wellness_config(conn, 10, channel_id=0)
    guild = _FakeGuild(guild_id=10)
    await _rebuild_active_list_for_guild(db_path, guild)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_rebuild_active_list_posts_and_pins_new_message(db_path: Path):
    with open_db(db_path) as conn:
        upsert_wellness_config(conn, 10, channel_id=999)
        # An opted-in committed user with a streak so the embed is non-trivial
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-30")

    channel = _FakeTextChannel(999)
    guild = _FakeGuild(guild_id=10, members={7: _FakeMember(7, "Alice")})
    guild.channels[999] = channel

    await _rebuild_active_list_for_guild(db_path, guild)  # type: ignore[arg-type]

    channel.send.assert_awaited_once()
    # The new message id should now be persisted in config
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT active_list_message_id FROM wellness_config WHERE guild_id = 10"
        ).fetchone()
    assert int(row["active_list_message_id"]) == 777


@pytest.mark.asyncio
async def test_rebuild_active_list_edits_existing_message(db_path: Path):
    """If active_list_message_id is set and fetch_message succeeds, edit not send."""
    with open_db(db_path) as conn:
        upsert_wellness_config(conn, 10, channel_id=999, active_list_message_id=888)

    channel = _FakeTextChannel(999)
    guild = _FakeGuild(guild_id=10)
    guild.channels[999] = channel

    await _rebuild_active_list_for_guild(db_path, guild)  # type: ignore[arg-type]

    channel.fetch_message.assert_awaited_once_with(888)
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_rebuild_active_list_skips_when_no_send_permission(db_path: Path):
    with open_db(db_path) as conn:
        upsert_wellness_config(conn, 10, channel_id=999)

    channel = _FakeTextChannel(999, perms=_FakePermissions(send_messages=False))
    guild = _FakeGuild(guild_id=10)
    guild.channels[999] = channel

    await _rebuild_active_list_for_guild(db_path, guild)  # type: ignore[arg-type]
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_rebuild_active_list_handles_fetch_notfound(db_path: Path):
    """When the stored message id is gone, the function falls back to posting a new one."""
    with open_db(db_path) as conn:
        upsert_wellness_config(conn, 10, channel_id=999, active_list_message_id=888)

    channel = _FakeTextChannel(999)
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))
    guild = _FakeGuild(guild_id=10)
    guild.channels[999] = channel

    await _rebuild_active_list_for_guild(db_path, guild)  # type: ignore[arg-type]
    channel.send.assert_awaited_once()


# ── _post_milestone_celebrations ─────────────────────────────────────


@pytest.mark.asyncio
async def test_post_milestone_celebrations_no_config_returns(db_path: Path):
    guild = _FakeGuild(guild_id=10)
    await _post_milestone_celebrations(db_path, guild)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_post_milestone_celebrations_seeds_celebrated_badge_silently(db_path: Path):
    """Seed badge (🌱) with 0 days should be marked celebrated without posting."""
    with open_db(db_path) as conn:
        upsert_wellness_config(conn, 10, channel_id=999)
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-30")  # default badge 🌱, days=0
        # celebrated_badge defaults to '' so it differs from '🌱'

    channel = _FakeTextChannel(999)
    guild = _FakeGuild(guild_id=10, members={7: _FakeMember(7)})
    guild.channels[999] = channel

    await _post_milestone_celebrations(db_path, guild)  # type: ignore[arg-type]

    channel.send.assert_not_called()
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT celebrated_badge FROM wellness_streaks "
            "WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()
    assert str(row["celebrated_badge"]) == "🌱"


@pytest.mark.asyncio
async def test_post_milestone_celebrations_celebrates_real_upgrade(db_path: Path):
    """An upgrade from seed → 🌟 emits a celebratory embed and marks it celebrated."""
    with open_db(db_path) as conn:
        upsert_wellness_config(conn, 10, channel_id=999)
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-25")
        conn.execute(
            "UPDATE wellness_streaks "
            "   SET current_days = 7, current_badge = '🌟', celebrated_badge = '🌱' "
            " WHERE guild_id = 10 AND user_id = 7"
        )

    channel = _FakeTextChannel(999)
    guild = _FakeGuild(guild_id=10, members={7: _FakeMember(7)})
    guild.channels[999] = channel

    await _post_milestone_celebrations(db_path, guild)  # type: ignore[arg-type]

    channel.send.assert_awaited_once()
    embed = channel.send.call_args.kwargs.get("embed")
    assert isinstance(embed, discord.Embed)
    assert embed.title is not None and "🌟" in embed.title

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT celebrated_badge FROM wellness_streaks WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()
    assert str(row["celebrated_badge"]) == "🌟"


@pytest.mark.asyncio
async def test_post_milestone_celebrations_skips_downgrades(db_path: Path):
    """Decay dropping 🌟 → 🌱 must NOT post (just silently mark celebrated)."""
    with open_db(db_path) as conn:
        upsert_wellness_config(conn, 10, channel_id=999)
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-25")
        conn.execute(
            "UPDATE wellness_streaks "
            "   SET current_days = 2, current_badge = '🌱', celebrated_badge = '🌟' "
            " WHERE guild_id = 10 AND user_id = 7"
        )

    channel = _FakeTextChannel(999)
    guild = _FakeGuild(guild_id=10, members={7: _FakeMember(7)})
    guild.channels[999] = channel

    await _post_milestone_celebrations(db_path, guild)  # type: ignore[arg-type]

    channel.send.assert_not_called()
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT celebrated_badge FROM wellness_streaks "
            "WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()
    assert str(row["celebrated_badge"]) == "🌱"


# ── _generate_and_send_weekly_report ─────────────────────────────────


@pytest.fixture
def _stub_ai(monkeypatch):
    """Make generate_weekly_encouragement return a deterministic offline string."""
    async def fake_ai(**_kwargs):
        return "Keep it up!"

    monkeypatch.setattr(scheduler, "generate_weekly_encouragement", fake_ai)


@freeze_time("2026-05-30 10:00:00")  # Saturday
@pytest.mark.asyncio
async def test_weekly_report_skipped_outside_sunday_morning(db_path: Path, _stub_ai):
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        user = conn.execute(
            "SELECT * FROM wellness_users WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()

    from bot_modules.services.wellness_service import WellnessUser

    wuser = WellnessUser.from_row(user)
    guild = _FakeGuild(guild_id=10, members={7: _FakeMember(7)})

    sent = await _generate_and_send_weekly_report(db_path, guild, wuser)  # type: ignore[arg-type]
    assert sent is False


@freeze_time("2026-05-31 10:00:00")  # Sunday 10am UTC
@pytest.mark.asyncio
async def test_weekly_report_sends_on_sunday_morning(db_path: Path, _stub_ai):
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-25")
        user_row = conn.execute(
            "SELECT * FROM wellness_users WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()

    from bot_modules.services.wellness_service import WellnessUser

    wuser = WellnessUser.from_row(user_row)
    member = _FakeMember(7, display_name="Alice")
    guild = _FakeGuild(guild_id=10, members={7: member})

    sent = await _generate_and_send_weekly_report(db_path, guild, wuser)  # type: ignore[arg-type]
    assert sent is True
    member.send.assert_awaited_once()
    # And a wellness_weekly_reports row should now exist
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM wellness_weekly_reports "
            "WHERE guild_id = 10 AND user_id = 7"
        ).fetchall()
    assert len(rows) == 1


@freeze_time("2026-05-31 10:00:00")
@pytest.mark.asyncio
async def test_weekly_report_skips_already_archived(db_path: Path, _stub_ai):
    """If has_weekly_report returns True the function bails early."""
    iso_year, iso_week, _ = _iso_week_for(datetime(2026, 5, 31, 10, 0))
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-25")
        conn.execute(
            "INSERT INTO wellness_weekly_reports "
            "(guild_id, user_id, iso_year, iso_week, week_start, report_json, ai_text, sent_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (10, 7, iso_year, iso_week, "2026-05-25", "{}", "ai", time.time()),
        )
        user_row = conn.execute(
            "SELECT * FROM wellness_users WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()

    from bot_modules.services.wellness_service import WellnessUser

    wuser = WellnessUser.from_row(user_row)
    member = _FakeMember(7)
    guild = _FakeGuild(guild_id=10, members={7: member})

    sent = await _generate_and_send_weekly_report(db_path, guild, wuser)  # type: ignore[arg-type]
    assert sent is False
    member.send.assert_not_called()


@freeze_time("2026-05-31 10:00:00")
@pytest.mark.asyncio
async def test_weekly_report_returns_false_if_member_missing(db_path: Path, _stub_ai):
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-25")
        user_row = conn.execute(
            "SELECT * FROM wellness_users WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()

    from bot_modules.services.wellness_service import WellnessUser

    wuser = WellnessUser.from_row(user_row)
    # No member added to guild → get_member returns None
    guild = _FakeGuild(guild_id=10)

    sent = await _generate_and_send_weekly_report(db_path, guild, wuser)  # type: ignore[arg-type]
    assert sent is False


@freeze_time("2026-05-31 10:00:00")
@pytest.mark.asyncio
async def test_weekly_report_swallows_dm_failure(db_path: Path, _stub_ai):
    """A closed DM channel leaves the archive row in place but does not raise."""
    with open_db(db_path) as conn:
        opt_in_user(conn, 10, 7, timezone="UTC")
        ensure_streak(conn, 10, 7, "2026-05-25")
        user_row = conn.execute(
            "SELECT * FROM wellness_users WHERE guild_id = 10 AND user_id = 7"
        ).fetchone()

    from bot_modules.services.wellness_service import WellnessUser

    wuser = WellnessUser.from_row(user_row)
    member = _FakeMember(7)
    member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no DM"))
    guild = _FakeGuild(guild_id=10, members={7: member})

    sent = await _generate_and_send_weekly_report(db_path, guild, wuser)  # type: ignore[arg-type]
    assert sent is True


# ── Loop wrappers — smoke ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wellness_tick_loop_exits_when_bot_closes(db_path: Path, monkeypatch):
    """The tick loop should perform one iteration and then exit when is_closed→True."""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.guilds = []
    # First check during the while-loop returns False (enter), then True (exit)
    bot.is_closed = MagicMock(side_effect=[False, True, True])

    # Replace asyncio.sleep so we don't actually wait 60s
    async def fast_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduler.asyncio, "sleep", fast_sleep)

    await asyncio.wait_for(wellness_tick_loop(bot, db_path), timeout=2.0)


@pytest.mark.asyncio
async def test_wellness_active_list_loop_exits(db_path: Path, monkeypatch):
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.guilds = []
    bot.is_closed = MagicMock(side_effect=[False, True, True])

    async def fast_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduler.asyncio, "sleep", fast_sleep)
    await asyncio.wait_for(wellness_active_list_loop(bot, db_path), timeout=2.0)


@pytest.mark.asyncio
async def test_wellness_weekly_report_loop_exits(db_path: Path, monkeypatch):
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.guilds = []
    bot.is_closed = MagicMock(side_effect=[False, True, True])

    async def fast_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduler.asyncio, "sleep", fast_sleep)
    await asyncio.wait_for(wellness_weekly_report_loop(bot, db_path), timeout=2.0)


@pytest.mark.asyncio
async def test_wellness_tick_loop_runs_nightly_at_00_05_utc(db_path: Path, monkeypatch):
    """Trigger the nightly maintenance branch (UTC hour == 0, minute >= 5)."""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.guilds = []
    bot.is_closed = MagicMock(side_effect=[False, True, True])

    async def fast_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduler.asyncio, "sleep", fast_sleep)

    nightly_calls: list[Path] = []

    async def fake_nightly(p: Path) -> None:
        nightly_calls.append(p)

    monkeypatch.setattr(scheduler, "_nightly_maintenance", fake_nightly)

    with freeze_time("2026-05-31 00:06:00"):
        await asyncio.wait_for(wellness_tick_loop(bot, db_path), timeout=2.0)

    assert nightly_calls == [db_path]


# ── Compute helper sanity ────────────────────────────────────────────


def test_iso_week_for_returns_str_date():
    """week_start_iso is an ISO date string."""
    _, _, week_start_iso = _iso_week_for(datetime(2026, 1, 1, 12, 0))
    # parse — must round-trip
    parsed = date.fromisoformat(week_start_iso)
    assert parsed.weekday() == 0  # Monday


def test_user_now_returns_aware_datetime():
    """sanity: imported symbol is callable + tz-aware (UTC fallback)."""
    now = user_now("Definitely/NotAZone")
    assert now.tzinfo is not None
    # And a real zone keeps offset
    real = user_now("America/Los_Angeles")
    assert real.utcoffset() is not None


def test_datetime_iso_helpers_smoke():
    """Sanity that freezegun cleanup hasn't broken time imports."""
    assert datetime.now(timezone.utc).year >= 2026
