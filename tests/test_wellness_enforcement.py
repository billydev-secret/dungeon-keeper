"""Tests for bot_modules.services.wellness_enforcement.

Covers the decide_action() decision tree, the slow-mode/friction helpers,
formatting + DM helpers, and the async wellness_on_message() hook with
mocked Discord objects. The strategy mirrors test_activity_graphs.py:
seed a migrated SQLite DB for the DB-touching helpers, and use
unittest.mock for the Discord side.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from freezegun import freeze_time

from bot_modules.core.db_utils import open_db
from bot_modules.services.wellness_enforcement import (
    Action,
    AWAY_DEFAULT_TEXT,
    EnforcementDecision,
    _arm_friction_for_caps,
    _bot_can_manage_messages,
    _cap_applies_to_channel,
    _category_id_for_channel,
    _effective_cap_limit,
    _enforcement_to_action,
    _format_seconds,
    _friction_blocks_message,
    _handle_away_mentions,
    _select_worst_action,
    _truncate,
    _try_dm,
    decide_action,
    wellness_on_message,
)
from bot_modules.services.wellness_service import (
    add_blackout,
    add_cap,
    add_exempt_channel,
    arm_slow_mode,
    get_slow_mode,
    get_wellness_user,
    increment_cap_counter,
    opt_in_user,
    update_away_message,
    update_user_settings,
    user_now,
)
from migrations import apply_migrations_sync


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """Sync sqlite3 connection with the full schema applied."""
    path = tmp_path / "wellness.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        yield conn


@pytest.fixture
def db_path(tmp_path):
    """Path to a migrated DB — for tests that open their own ctx.open_db."""
    path = tmp_path / "wellness.db"
    apply_migrations_sync(path)
    return path


def _make_user(conn, *, guild_id: int = 100, user_id: int = 200, **overrides):
    """opt_in_user + apply any setting overrides via update_user_settings."""
    opt_in_user(
        conn,
        guild_id,
        user_id,
        timezone=overrides.pop("timezone_", "UTC"),
        enforcement_level=overrides.pop("enforcement_level", "gradual"),
    )
    if overrides:
        update_user_settings(conn, guild_id, user_id, **overrides)
    user = get_wellness_user(conn, guild_id, user_id)
    assert user is not None
    return user


def _make_message(
    *,
    guild_id: int = 100,
    author_id: int = 200,
    channel_id: int = 300,
    category_id: int | None = None,
    parent_id: int | None = None,
    mentions: list | None = None,
    is_bot: bool = False,
    content: str = "hello",
    can_manage: bool = True,
    can_send: bool = True,
):
    """Build a minimal MagicMock that looks like a discord.Message."""
    msg = MagicMock(spec=discord.Message)
    msg.id = 1
    msg.content = content

    author = MagicMock()
    author.id = author_id
    author.bot = is_bot
    author.display_name = f"user-{author_id}"
    author.mention = f"<@{author_id}>"
    author.send = AsyncMock()
    msg.author = author

    guild = MagicMock()
    guild.id = guild_id
    me = MagicMock()
    guild.me = me
    msg.guild = guild

    channel = MagicMock()
    channel.id = channel_id
    channel.name = f"chan-{channel_id}"
    if category_id is not None:
        category = MagicMock()
        category.id = category_id
        channel.category = category
    else:
        channel.category = None
    if parent_id is not None:
        parent = MagicMock()
        parent.id = parent_id
        parent.category = None
        channel.parent = parent
    else:
        channel.parent = None

    perms = MagicMock()
    perms.manage_messages = can_manage
    perms.send_messages = can_send
    channel.permissions_for = MagicMock(return_value=perms)
    channel.send = AsyncMock(return_value=MagicMock())

    msg.channel = channel
    msg.mentions = mentions or []
    msg.delete = AsyncMock()

    return msg


def _make_ctx(db_path):
    """A minimal ctx that exposes open_db() as the real context manager."""
    ctx = MagicMock()
    ctx.open_db = lambda: open_db(db_path)
    return ctx


# ── _enforcement_to_action ───────────────────────────────────────────


def test_enforcement_to_action_gentle_maps_to_nudge():
    assert _enforcement_to_action("gentle") == Action.NUDGE


def test_enforcement_to_action_cooldown_maps_to_cooldown():
    assert _enforcement_to_action("cooldown") == Action.COOLDOWN


def test_enforcement_to_action_slow_mode_maps_to_friction():
    assert _enforcement_to_action("slow_mode") == Action.FRICTION


def test_enforcement_to_action_gradual_maps_to_friction():
    assert _enforcement_to_action("gradual") == Action.FRICTION


def test_enforcement_to_action_unknown_falls_back_to_friction():
    assert _enforcement_to_action("garbage") == Action.FRICTION


# ── _category_id_for_channel ─────────────────────────────────────────


def test_category_id_uses_direct_category_first():
    channel = MagicMock()
    channel.category = MagicMock(id=42)
    channel.parent = None
    assert _category_id_for_channel(channel) == 42


def test_category_id_falls_back_to_parent_category_for_threads():
    channel = MagicMock()
    channel.category = None
    parent = MagicMock()
    parent.category = MagicMock(id=99)
    channel.parent = parent
    assert _category_id_for_channel(channel) == 99


def test_category_id_returns_zero_when_no_category():
    channel = MagicMock()
    channel.category = None
    channel.parent = None
    assert _category_id_for_channel(channel) == 0


def test_category_id_returns_zero_when_parent_has_no_category():
    channel = MagicMock()
    channel.category = None
    parent = MagicMock()
    parent.category = None
    channel.parent = parent
    assert _category_id_for_channel(channel) == 0


# ── _cap_applies_to_channel ──────────────────────────────────────────


def _cap(scope: str, target: int = 0):
    cap = MagicMock()
    cap.scope = scope
    cap.scope_target_id = target
    return cap


def test_cap_applies_global_always_matches():
    channel = MagicMock(id=1, parent=None)
    assert _cap_applies_to_channel(_cap("global"), channel) is True


def test_cap_applies_channel_matches_by_id():
    channel = MagicMock(id=500, parent=None)
    assert _cap_applies_to_channel(_cap("channel", 500), channel) is True


def test_cap_applies_channel_matches_via_thread_parent():
    parent = MagicMock()
    parent.id = 500
    channel = MagicMock()
    channel.id = 999
    channel.parent = parent
    assert _cap_applies_to_channel(_cap("channel", 500), channel) is True


def test_cap_applies_channel_returns_false_when_no_match():
    channel = MagicMock()
    channel.id = 42
    channel.parent = None
    assert _cap_applies_to_channel(_cap("channel", 500), channel) is False


def test_cap_applies_category_uses_category_id():
    channel = MagicMock()
    channel.id = 1
    category = MagicMock()
    category.id = 77
    channel.category = category
    channel.parent = None
    assert _cap_applies_to_channel(_cap("category", 77), channel) is True


def test_cap_applies_voice_scope_returns_false():
    channel = MagicMock()
    channel.id = 1
    channel.parent = None
    assert _cap_applies_to_channel(_cap("voice"), channel) is False


# ── _select_worst_action ─────────────────────────────────────────────


def test_select_worst_picks_max_overage_under_cap():
    assert (
        _select_worst_action(Action.FRICTION, [Action.NUDGE, Action.COOLDOWN])
        == Action.COOLDOWN
    )


def test_select_worst_clamps_to_level_max():
    """If user only allows NUDGE, FRICTION-worthy overage downgrades to NUDGE."""
    assert (
        _select_worst_action(Action.NUDGE, [Action.FRICTION, Action.COOLDOWN])
        == Action.NUDGE
    )


def test_select_worst_empty_returns_allow():
    assert _select_worst_action(Action.FRICTION, []) == Action.ALLOW


# ── _format_seconds ──────────────────────────────────────────────────


def test_format_seconds_under_minute():
    assert _format_seconds(45) == "45s"


def test_format_seconds_minutes():
    assert _format_seconds(125) == "2:05"


def test_format_seconds_hours():
    assert _format_seconds(3675) == "1:01:15"


def test_format_seconds_negative_clamps_to_zero():
    assert _format_seconds(-5) == "0s"


# ── _truncate ────────────────────────────────────────────────────────


def test_truncate_short_string_passes_through():
    assert _truncate("hi", 10) == "hi"


def test_truncate_long_string_uses_ellipsis():
    out = _truncate("a" * 20, 5)
    assert out.endswith("…")
    assert len(out) == 5


def test_truncate_empty_returns_placeholder():
    assert _truncate("", 5) == "(no text)"
    assert _truncate(None, 5) == "(no text)"


# ── _effective_cap_limit ─────────────────────────────────────────────


def test_effective_cap_limit_returns_cap_limit_when_no_buckets():
    cap = MagicMock()
    cap.bucket_limits = None
    cap.cap_limit = 10
    cap.window = "daily"
    assert _effective_cap_limit(cap, datetime(2026, 5, 31, 12, 0)) == 10


def test_effective_cap_limit_uses_hour_bucket_for_daily():
    cap = MagicMock()
    cap.bucket_limits = [i for i in range(24)]  # bucket[12] == 12
    cap.cap_limit = 100
    cap.window = "daily"
    assert _effective_cap_limit(cap, datetime(2026, 5, 31, 12, 30)) == 12


def test_effective_cap_limit_uses_weekday_bucket_for_weekly():
    cap = MagicMock()
    cap.bucket_limits = [10, 20, 30, 40, 50, 60, 70]  # Mon..Sun
    cap.cap_limit = 100
    cap.window = "weekly"
    # 2026-06-01 is a Monday
    assert _effective_cap_limit(cap, datetime(2026, 6, 1, 12, 0)) == 10
    # 2026-06-02 is a Tuesday
    assert _effective_cap_limit(cap, datetime(2026, 6, 2, 12, 0)) == 20


def test_effective_cap_limit_hourly_window_ignores_buckets():
    cap = MagicMock()
    cap.bucket_limits = [99, 99]
    cap.cap_limit = 7
    cap.window = "hourly"
    assert _effective_cap_limit(cap, datetime(2026, 5, 31, 12, 0)) == 7


def test_effective_cap_limit_out_of_range_falls_back():
    cap = MagicMock()
    cap.bucket_limits = [1, 2, 3]  # only 3 entries
    cap.cap_limit = 42
    cap.window = "daily"
    # Hour 12 is out of range → fall back to cap_limit
    assert _effective_cap_limit(cap, datetime(2026, 5, 31, 12, 0)) == 42


# ── _friction_blocks_message ─────────────────────────────────────────


def test_friction_blocks_when_no_slow_mode_state(db_conn):
    blocked, wait = _friction_blocks_message(db_conn, 1, 2, 60, time.time())
    assert blocked is False
    assert wait == 0.0


def test_friction_blocks_when_window_expired(db_conn):
    now = 1_000_000.0
    arm_slow_mode(
        db_conn, 1, 2, triggered_by_cap_id=0, triggered_window_start=0,
        active_until_ts=now - 100,
    )
    blocked, _ = _friction_blocks_message(db_conn, 1, 2, 60, now)
    assert blocked is False


def test_friction_blocks_when_rate_limit_active(db_conn):
    now = 1_000_000.0
    arm_slow_mode(
        db_conn, 1, 2, triggered_by_cap_id=0, triggered_window_start=0,
        active_until_ts=now + 3600,
    )
    # Last message 10s ago, rate 60s → blocked, 50s left.
    db_conn.execute(
        "UPDATE wellness_slow_mode SET last_message_ts = ? WHERE guild_id = ? AND user_id = ?",
        (now - 10, 1, 2),
    )
    blocked, wait = _friction_blocks_message(db_conn, 1, 2, 60, now)
    assert blocked is True
    assert 49 < wait <= 50


def test_friction_does_not_block_after_rate_elapsed(db_conn):
    now = 1_000_000.0
    arm_slow_mode(
        db_conn, 1, 2, triggered_by_cap_id=0, triggered_window_start=0,
        active_until_ts=now + 3600,
    )
    db_conn.execute(
        "UPDATE wellness_slow_mode SET last_message_ts = ? WHERE guild_id = ? AND user_id = ?",
        (now - 120, 1, 2),
    )
    blocked, _ = _friction_blocks_message(db_conn, 1, 2, 60, now)
    assert blocked is False


# ── _arm_friction_for_caps ───────────────────────────────────────────


@freeze_time("2026-05-31 12:30:00")
def test_arm_friction_picks_latest_window_end(db_conn):
    now_local = datetime(2026, 5, 31, 12, 30, tzinfo=timezone.utc)
    cap_id_hourly = add_cap(
        db_conn, 1, 2, label="h", scope="global", scope_target_id=0,
        window="hourly", cap_limit=5,
    )
    cap_id_daily = add_cap(
        db_conn, 1, 2, label="d", scope="global", scope_target_id=0,
        window="daily", cap_limit=20,
    )

    # Use real WellnessCap rows so attribute access is correct.
    from bot_modules.services.wellness_service import list_caps
    caps = list_caps(db_conn, 1, 2)

    _arm_friction_for_caps(db_conn, 1, 2, caps, now_local, daily_reset_hour=0)

    sm = get_slow_mode(db_conn, 1, 2)
    assert sm is not None
    # Latest end is the daily window end → tomorrow midnight UTC
    expected_end = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    assert abs(sm.active_until_ts - expected_end) < 1
    assert sm.triggered_by_cap_id == cap_id_daily
    # Sanity: hourly cap_id is not the trigger
    assert sm.triggered_by_cap_id != cap_id_hourly


def test_arm_friction_noop_for_empty_list(db_conn):
    now_local = datetime(2026, 5, 31, 12, 30, tzinfo=timezone.utc)
    _arm_friction_for_caps(db_conn, 1, 2, [], now_local, daily_reset_hour=0)
    assert get_slow_mode(db_conn, 1, 2) is None


# ── _bot_can_manage_messages ─────────────────────────────────────────


def test_bot_can_manage_returns_perm_value():
    msg = _make_message(can_manage=True)
    assert _bot_can_manage_messages(msg) is True

    msg2 = _make_message(can_manage=False)
    assert _bot_can_manage_messages(msg2) is False


def test_bot_can_manage_returns_false_without_guild():
    msg = MagicMock(spec=discord.Message)
    msg.guild = None
    assert _bot_can_manage_messages(msg) is False


def test_bot_can_manage_returns_false_when_guild_me_is_none():
    msg = MagicMock(spec=discord.Message)
    guild = MagicMock()
    guild.me = None
    msg.guild = guild
    assert _bot_can_manage_messages(msg) is False


# ── _try_dm ──────────────────────────────────────────────────────────


async def test_try_dm_returns_true_on_success():
    user = MagicMock()
    user.send = AsyncMock()
    assert await _try_dm(user, content="hi") is True
    user.send.assert_awaited_once_with(content="hi")


async def test_try_dm_returns_false_on_forbidden():
    user = MagicMock()
    user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    assert await _try_dm(user, content="hi") is False


async def test_try_dm_returns_false_on_http_error():
    user = MagicMock()
    user.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))
    assert await _try_dm(user, content="hi") is False


async def test_try_dm_with_embed_only_omits_content_kwarg():
    user = MagicMock()
    user.send = AsyncMock()
    embed = MagicMock()
    await _try_dm(user, embed=embed)
    kwargs = user.send.await_args.kwargs
    assert "embed" in kwargs
    assert "content" not in kwargs


# ── decide_action ────────────────────────────────────────────────────


def test_decide_action_inactive_user_allows(db_conn):
    _make_user(db_conn)
    db_conn.execute(
        "UPDATE wellness_users SET opted_in_at = NULL WHERE guild_id = ? AND user_id = ?",
        (100, 200),
    )
    user = get_wellness_user(db_conn, 100, 200)
    assert user is not None
    msg = _make_message()
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.ALLOW
    assert decision.reason == "inactive"


def test_decide_action_paused_user_allows(db_conn):
    _make_user(db_conn)
    db_conn.execute(
        "UPDATE wellness_users SET paused_until = ? WHERE guild_id = ? AND user_id = ?",
        (time.time() + 3600, 100, 200),
    )
    user = get_wellness_user(db_conn, 100, 200)
    assert user is not None
    msg = _make_message()
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.ALLOW
    assert decision.reason == "inactive"


def test_decide_action_no_caps_no_blackouts_allows(db_conn):
    user = _make_user(db_conn)
    msg = _make_message()
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.ALLOW
    assert decision.reason == "no_caps"


def test_decide_action_cap_not_applicable_allows(db_conn):
    user = _make_user(db_conn)
    add_cap(
        db_conn, 100, 200, label="c", scope="channel", scope_target_id=999,
        window="hourly", cap_limit=5,
    )
    msg = _make_message(channel_id=12345)
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.ALLOW
    assert decision.reason == "not_applicable"


def test_decide_action_exempt_channel_allows(db_conn):
    user = _make_user(db_conn)
    add_cap(
        db_conn, 100, 200, label="c", scope="global", scope_target_id=0,
        window="hourly", cap_limit=5, exclude_exempt=True,
    )
    add_exempt_channel(db_conn, 100, 300)
    msg = _make_message(channel_id=300)
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.ALLOW
    assert decision.reason == "not_applicable"


@freeze_time("2026-05-31 12:30:00")
def test_decide_action_under_cap_allows_and_increments(db_conn):
    user = _make_user(db_conn)
    cap_id = add_cap(
        db_conn, 100, 200, label="g", scope="global", scope_target_id=0,
        window="hourly", cap_limit=5,
    )
    msg = _make_message()
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.ALLOW
    assert decision.reason == "under_cap"
    # Counter incremented to 1
    from bot_modules.services.wellness_service import get_cap_counter, window_start_epoch
    ws = window_start_epoch("hourly", user_now("UTC"), 0)
    assert get_cap_counter(db_conn, cap_id, ws) == 1


@freeze_time("2026-05-31 12:30:00")
def test_decide_action_first_overage_emits_nudge(db_conn):
    user = _make_user(db_conn)  # gradual → cap = FRICTION
    cap_id = add_cap(
        db_conn, 100, 200, label="g", scope="global", scope_target_id=0,
        window="hourly", cap_limit=3,
    )
    from bot_modules.services.wellness_service import window_start_epoch
    ws = window_start_epoch("hourly", user_now("UTC"), 0)
    # Pre-fill counter so the cap is already at limit.
    for _ in range(3):
        increment_cap_counter(db_conn, cap_id, ws)

    msg = _make_message()
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.NUDGE
    assert decision.reason == "cap_overage"
    assert len(decision.cap_hits) == 1


@freeze_time("2026-05-31 12:30:00")
def test_decide_action_escalates_to_cooldown_on_second_overage(db_conn):
    user = _make_user(db_conn)
    cap_id = add_cap(
        db_conn, 100, 200, label="g", scope="global", scope_target_id=0,
        window="hourly", cap_limit=2,
    )
    from bot_modules.services.wellness_service import window_start_epoch
    ws = window_start_epoch("hourly", user_now("UTC"), 0)
    for _ in range(2):
        increment_cap_counter(db_conn, cap_id, ws)

    msg = _make_message()
    decide_action(db_conn, user, msg)  # 1st overage → NUDGE
    decision = decide_action(db_conn, user, msg)  # 2nd → COOLDOWN
    assert decision.action == Action.COOLDOWN


@freeze_time("2026-05-31 12:30:00")
def test_decide_action_escalates_to_friction_on_third_overage(db_conn):
    user = _make_user(db_conn)
    cap_id = add_cap(
        db_conn, 100, 200, label="g", scope="global", scope_target_id=0,
        window="hourly", cap_limit=1,
    )
    from bot_modules.services.wellness_service import window_start_epoch
    ws = window_start_epoch("hourly", user_now("UTC"), 0)
    increment_cap_counter(db_conn, cap_id, ws)

    msg = _make_message()
    for _ in range(2):
        decide_action(db_conn, user, msg)
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.FRICTION


@freeze_time("2026-05-31 12:30:00")
def test_decide_action_overage_clamps_to_gentle_user(db_conn):
    """A gentle user can never escalate past NUDGE even on the 5th overage."""
    user = _make_user(db_conn, enforcement_level="gentle")
    cap_id = add_cap(
        db_conn, 100, 200, label="g", scope="global", scope_target_id=0,
        window="hourly", cap_limit=1,
    )
    from bot_modules.services.wellness_service import window_start_epoch
    ws = window_start_epoch("hourly", user_now("UTC"), 0)
    increment_cap_counter(db_conn, cap_id, ws)

    msg = _make_message()
    decision = decide_action(db_conn, user, msg)
    for _ in range(3):
        decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.NUDGE


# ── decide_action: blackouts ─────────────────────────────────────────


@freeze_time("2026-05-31 23:30:00")  # Sunday 23:30 UTC
def test_decide_action_blackout_gradual_starts_at_nudge(db_conn):
    user = _make_user(db_conn, timezone_="UTC", enforcement_level="gradual")
    # 23:00-07:00 every day = night-owl-like blackout
    add_blackout(
        db_conn, 100, 200, name="night",
        start_minute=23 * 60, end_minute=7 * 60, days_mask=127,
    )
    msg = _make_message()
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.NUDGE
    assert decision.reason == "blackout"
    assert decision.blackout is not None


@freeze_time("2026-05-31 23:30:00")
def test_decide_action_blackout_uses_cooldown_for_cooldown_user(db_conn):
    user = _make_user(db_conn, enforcement_level="cooldown")
    add_blackout(
        db_conn, 100, 200, name="night",
        start_minute=23 * 60, end_minute=7 * 60, days_mask=127,
    )
    msg = _make_message()
    decision = decide_action(db_conn, user, msg)
    assert decision.action == Action.COOLDOWN


# ── wellness_on_message ──────────────────────────────────────────────


async def test_on_message_bot_author_returns_false(db_path):
    ctx = _make_ctx(db_path)
    msg = _make_message(is_bot=True)
    assert await wellness_on_message(ctx, msg) is False


async def test_on_message_dm_returns_false(db_path):
    ctx = _make_ctx(db_path)
    msg = _make_message()
    msg.guild = None
    assert await wellness_on_message(ctx, msg) is False


async def test_on_message_no_wellness_user_returns_false(db_path):
    ctx = _make_ctx(db_path)
    msg = _make_message()
    assert await wellness_on_message(ctx, msg) is False


async def test_on_message_inactive_user_returns_false(db_path):
    with open_db(db_path) as conn:
        _make_user(conn)
        conn.execute(
            "UPDATE wellness_users SET opted_out_at = ? WHERE guild_id = ? AND user_id = ?",
            (time.time(), 100, 200),
        )
    ctx = _make_ctx(db_path)
    msg = _make_message()
    assert await wellness_on_message(ctx, msg) is False


async def test_on_message_no_caps_allows(db_path):
    with open_db(db_path) as conn:
        _make_user(conn)
    ctx = _make_ctx(db_path)
    msg = _make_message()
    assert await wellness_on_message(ctx, msg) is False


@freeze_time("2026-05-31 12:30:00")
async def test_on_message_nudge_returns_false_and_records_streak(db_path):
    with open_db(db_path) as conn:
        _make_user(conn, notifications_pref="dm")
        cap_id = add_cap(
            conn, 100, 200, label="g", scope="global", scope_target_id=0,
            window="hourly", cap_limit=1,
        )
        from bot_modules.services.wellness_service import window_start_epoch
        ws = window_start_epoch("hourly", user_now("UTC"), 0)
        increment_cap_counter(conn, cap_id, ws)

    ctx = _make_ctx(db_path)
    msg = _make_message()
    result = await wellness_on_message(ctx, msg)
    assert result is False  # nudge does not consume
    msg.author.send.assert_awaited()  # DM was sent
    # Streak violation row exists
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT last_violation_date FROM wellness_streaks WHERE guild_id = ? AND user_id = ?",
            (100, 200),
        ).fetchone()
        assert row is not None
        assert row["last_violation_date"] == "2026-05-31"


@freeze_time("2026-05-31 12:30:00")
async def test_on_message_cooldown_sets_cooldown_until(db_path):
    with open_db(db_path) as conn:
        _make_user(conn, notifications_pref="dm")
        cap_id = add_cap(
            conn, 100, 200, label="g", scope="global", scope_target_id=0,
            window="hourly", cap_limit=1,
        )
        from bot_modules.services.wellness_service import window_start_epoch
        ws = window_start_epoch("hourly", user_now("UTC"), 0)
        increment_cap_counter(conn, cap_id, ws)
        # First overage → NUDGE
        from bot_modules.services.wellness_service import increment_cap_overage
        increment_cap_overage(conn, cap_id, ws)  # pre-bump to 1

    ctx = _make_ctx(db_path)
    msg = _make_message()
    # This will be the 2nd overage → COOLDOWN
    result = await wellness_on_message(ctx, msg)
    assert result is False

    with open_db(db_path) as conn:
        u = get_wellness_user(conn, 100, 200)
        assert u is not None
        assert u.cooldown_until is not None
        assert u.cooldown_until > time.time()


@freeze_time("2026-05-31 12:30:00")
async def test_on_message_friction_deletes_when_dm_ok(db_path):
    with open_db(db_path) as conn:
        _make_user(conn)
        cap_id = add_cap(
            conn, 100, 200, label="g", scope="global", scope_target_id=0,
            window="hourly", cap_limit=1,
        )
        from bot_modules.services.wellness_service import (
            increment_cap_overage,
            window_start_epoch,
        )
        ws = window_start_epoch("hourly", user_now("UTC"), 0)
        increment_cap_counter(conn, cap_id, ws)
        # Pre-bump overages to 2 → next will hit FRICTION
        increment_cap_overage(conn, cap_id, ws)
        increment_cap_overage(conn, cap_id, ws)

    ctx = _make_ctx(db_path)
    msg = _make_message(can_manage=True)
    result = await wellness_on_message(ctx, msg)
    assert result is True
    msg.author.send.assert_awaited()
    msg.delete.assert_awaited()

    # Slow mode armed
    with open_db(db_path) as conn:
        sm = get_slow_mode(conn, 100, 200)
        assert sm is not None
        assert sm.active_until_ts > time.time()


@freeze_time("2026-05-31 12:30:00")
async def test_on_message_friction_degrades_to_nudge_when_cant_delete(db_path):
    with open_db(db_path) as conn:
        _make_user(conn, notifications_pref="dm")
        cap_id = add_cap(
            conn, 100, 200, label="g", scope="global", scope_target_id=0,
            window="hourly", cap_limit=1,
        )
        from bot_modules.services.wellness_service import (
            increment_cap_overage,
            window_start_epoch,
        )
        ws = window_start_epoch("hourly", user_now("UTC"), 0)
        increment_cap_counter(conn, cap_id, ws)
        increment_cap_overage(conn, cap_id, ws)
        increment_cap_overage(conn, cap_id, ws)

    ctx = _make_ctx(db_path)
    msg = _make_message(can_manage=False)
    result = await wellness_on_message(ctx, msg)
    assert result is False  # degraded to nudge → not consumed
    msg.delete.assert_not_awaited()


@freeze_time("2026-05-31 12:30:00")
async def test_on_message_friction_skipped_when_dm_closed(db_path):
    with open_db(db_path) as conn:
        _make_user(conn)
        cap_id = add_cap(
            conn, 100, 200, label="g", scope="global", scope_target_id=0,
            window="hourly", cap_limit=1,
        )
        from bot_modules.services.wellness_service import (
            increment_cap_overage,
            window_start_epoch,
        )
        ws = window_start_epoch("hourly", user_now("UTC"), 0)
        increment_cap_counter(conn, cap_id, ws)
        increment_cap_overage(conn, cap_id, ws)
        increment_cap_overage(conn, cap_id, ws)

    ctx = _make_ctx(db_path)
    msg = _make_message(can_manage=True)
    msg.author.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "x"))
    result = await wellness_on_message(ctx, msg)
    assert result is False
    msg.delete.assert_not_awaited()


async def test_on_message_blocked_by_active_slow_mode(db_path):
    now = time.time()
    with open_db(db_path) as conn:
        _make_user(conn)
        arm_slow_mode(
            conn, 100, 200, triggered_by_cap_id=0, triggered_window_start=0,
            active_until_ts=now + 3600,
        )
        conn.execute(
            "UPDATE wellness_slow_mode SET last_message_ts = ? WHERE guild_id = ? AND user_id = ?",
            (now - 5, 100, 200),
        )

    ctx = _make_ctx(db_path)
    msg = _make_message(can_manage=True)
    result = await wellness_on_message(ctx, msg)
    assert result is True
    msg.delete.assert_awaited()


async def test_on_message_slow_mode_skipped_when_dm_closed(db_path):
    now = time.time()
    with open_db(db_path) as conn:
        _make_user(conn)
        arm_slow_mode(
            conn, 100, 200, triggered_by_cap_id=0, triggered_window_start=0,
            active_until_ts=now + 3600,
        )
        conn.execute(
            "UPDATE wellness_slow_mode SET last_message_ts = ? WHERE guild_id = ? AND user_id = ?",
            (now - 5, 100, 200),
        )

    ctx = _make_ctx(db_path)
    msg = _make_message(can_manage=True)
    msg.author.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "x"))
    result = await wellness_on_message(ctx, msg)
    assert result is False
    msg.delete.assert_not_awaited()


async def test_on_message_slow_mode_skipped_without_manage_perms(db_path):
    now = time.time()
    with open_db(db_path) as conn:
        _make_user(conn)
        arm_slow_mode(
            conn, 100, 200, triggered_by_cap_id=0, triggered_window_start=0,
            active_until_ts=now + 3600,
        )
        conn.execute(
            "UPDATE wellness_slow_mode SET last_message_ts = ? WHERE guild_id = ? AND user_id = ?",
            (now - 5, 100, 200),
        )

    ctx = _make_ctx(db_path)
    msg = _make_message(can_manage=False)
    result = await wellness_on_message(ctx, msg)
    assert result is False


# ── Away-mention auto-reply ──────────────────────────────────────────


async def test_handle_away_no_mentions_noop(db_path):
    ctx = _make_ctx(db_path)
    msg = _make_message(mentions=[])
    await _handle_away_mentions(ctx, msg)
    msg.channel.send.assert_not_called()


async def test_handle_away_user_with_away_off_skipped(db_path):
    mention = MagicMock()
    mention.id = 500
    mention.bot = False
    mention.display_name = "AwayUser"
    mention.mention = "<@500>"

    with open_db(db_path) as conn:
        _make_user(conn, user_id=500)
        # away_enabled defaults to 0

    ctx = _make_ctx(db_path)
    msg = _make_message(mentions=[mention])
    await _handle_away_mentions(ctx, msg)
    msg.channel.send.assert_not_called()


async def test_handle_away_sends_reply_for_away_user(db_path):
    mention = MagicMock()
    mention.id = 500
    mention.bot = False
    mention.display_name = "AwayUser"
    mention.mention = "<@500>"

    with open_db(db_path) as conn:
        _make_user(conn, user_id=500)
        update_away_message(conn, 100, 500, enabled=True, message="back soon")

    ctx = _make_ctx(db_path)
    msg = _make_message(mentions=[mention])
    await _handle_away_mentions(ctx, msg)
    msg.channel.send.assert_awaited_once()


async def test_handle_away_uses_default_text_when_blank(db_path):
    mention = MagicMock()
    mention.id = 500
    mention.bot = False
    mention.display_name = "AwayUser"
    mention.mention = "<@500>"

    with open_db(db_path) as conn:
        _make_user(conn, user_id=500)
        update_away_message(conn, 100, 500, enabled=True, message="")

    ctx = _make_ctx(db_path)
    msg = _make_message(mentions=[mention])
    await _handle_away_mentions(ctx, msg)
    call = msg.channel.send.await_args
    embed = call.kwargs["embed"]
    assert AWAY_DEFAULT_TEXT in embed.description


async def test_handle_away_skips_bot_mentions(db_path):
    mention = MagicMock()
    mention.id = 500
    mention.bot = True
    mention.display_name = "BotUser"

    with open_db(db_path) as conn:
        _make_user(conn, user_id=500)
        update_away_message(conn, 100, 500, enabled=True, message="x")

    ctx = _make_ctx(db_path)
    msg = _make_message(mentions=[mention])
    await _handle_away_mentions(ctx, msg)
    msg.channel.send.assert_not_called()


async def test_handle_away_skips_self_mention(db_path):
    mention = MagicMock()
    mention.id = 200  # same as author_id default
    mention.bot = False
    mention.display_name = "Me"

    with open_db(db_path) as conn:
        _make_user(conn, user_id=200)
        update_away_message(conn, 100, 200, enabled=True, message="x")

    ctx = _make_ctx(db_path)
    msg = _make_message(mentions=[mention])
    await _handle_away_mentions(ctx, msg)
    msg.channel.send.assert_not_called()


async def test_handle_away_rate_limited(db_path):
    mention = MagicMock()
    mention.id = 500
    mention.bot = False
    mention.display_name = "AwayUser"
    mention.mention = "<@500>"

    with open_db(db_path) as conn:
        _make_user(conn, user_id=500)
        update_away_message(conn, 100, 500, enabled=True, message="x")
        # Pretend an away reply was sent 1 minute ago in this channel
        from bot_modules.services.wellness_service import record_away_sent
        record_away_sent(conn, 100, 500, 300, time.time() - 60)

    ctx = _make_ctx(db_path)
    msg = _make_message(mentions=[mention])
    await _handle_away_mentions(ctx, msg)
    msg.channel.send.assert_not_called()


async def test_handle_away_skipped_when_cannot_send(db_path):
    mention = MagicMock()
    mention.id = 500
    mention.bot = False
    mention.display_name = "AwayUser"
    mention.mention = "<@500>"

    with open_db(db_path) as conn:
        _make_user(conn, user_id=500)
        update_away_message(conn, 100, 500, enabled=True, message="x")

    ctx = _make_ctx(db_path)
    msg = _make_message(mentions=[mention], can_send=False)
    await _handle_away_mentions(ctx, msg)
    msg.channel.send.assert_not_called()


async def test_handle_away_deduplicates_same_mention(db_path):
    mention = MagicMock()
    mention.id = 500
    mention.bot = False
    mention.display_name = "AwayUser"
    mention.mention = "<@500>"

    with open_db(db_path) as conn:
        _make_user(conn, user_id=500)
        update_away_message(conn, 100, 500, enabled=True, message="x")

    ctx = _make_ctx(db_path)
    # Same person mentioned twice
    msg = _make_message(mentions=[mention, mention])
    await _handle_away_mentions(ctx, msg)
    # Only one reply, not two
    assert msg.channel.send.await_count == 1


# ── EnforcementDecision dataclass smoke test ─────────────────────────


def test_enforcement_decision_default_blackout_and_reason():
    d = EnforcementDecision(action=Action.ALLOW, cap_hits=[])
    assert d.blackout is None
    assert d.reason == ""
    assert d.action == Action.ALLOW
