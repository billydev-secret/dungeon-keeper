"""Tests for ``bot_modules.privacy.logic`` — the pure helpers behind the
``/delete_me`` and ``/delete_user`` flows.

The cog still owns the network I/O (channel.delete_messages, thread.edit, DM
sends). These tests focus on what's testable without Discord: bucketing
message rows per channel, partitioning by the 14-day bulk-delete window,
chunking for the 100-per-call cap, and the text formatting the cog uses to
report progress and completion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.privacy.logic import (
    FOURTEEN_DAYS,
    MODE_MEDIA,
    chunk_for_bulk_delete,
    group_messages_by_channel,
    is_forum_thread,
    partition_by_bulk_delete_window,
    render_deletion_summary,
    render_empty_summary,
    render_progress_bar,
    render_scan_status,
    should_throttle,
)


# ── is_forum_thread ────────────────────────────────────────────────────


def test_is_forum_thread_returns_false_for_none():
    assert is_forum_thread(None) is False


def test_is_forum_thread_returns_false_for_text_channel():
    ch = MagicMock(spec=discord.TextChannel)
    assert is_forum_thread(ch) is False


def test_is_forum_thread_returns_false_for_regular_thread():
    """A Thread whose parent is a TextChannel is *not* a forum thread."""
    thread = MagicMock(spec=discord.Thread)
    thread.parent = MagicMock(spec=discord.TextChannel)
    assert is_forum_thread(thread) is False


def test_is_forum_thread_returns_true_for_thread_under_forum():
    thread = MagicMock(spec=discord.Thread)
    thread.parent = MagicMock(spec=discord.ForumChannel)
    assert is_forum_thread(thread) is True


# ── group_messages_by_channel ──────────────────────────────────────────


def test_group_messages_by_channel_empty():
    assert group_messages_by_channel([]) == {}


def test_group_messages_by_channel_groups_per_channel():
    rows = [
        (1001, 10),
        (1002, 10),
        (2001, 20),
        (1003, 10),
        (2002, 20),
        (3001, 30),
    ]
    grouped = group_messages_by_channel(rows)
    assert grouped[10] == [1001, 1002, 1003]
    assert grouped[20] == [2001, 2002]
    assert grouped[30] == [3001]
    assert set(grouped.keys()) == {10, 20, 30}


def test_group_messages_by_channel_preserves_input_order_within_channel():
    rows = [(5, 1), (1, 1), (9, 1), (3, 1)]
    grouped = group_messages_by_channel(rows)
    assert grouped[1] == [5, 1, 9, 3]


# ── partition_by_bulk_delete_window ────────────────────────────────────


_DISCORD_EPOCH = 1420070400000  # 2015-01-01 UTC, in ms


def _snowflake_at(dt: datetime) -> int:
    """Build a snowflake ID for a given datetime (UTC).

    Mirrors Discord's algorithm: ``(ms_since_epoch << 22)`` is enough for
    ``discord.utils.snowflake_time`` to round-trip.
    """
    ms = int(dt.timestamp() * 1000) - _DISCORD_EPOCH
    return ms << 22


def test_partition_uses_default_now_when_none():
    """Without an explicit ``now``, partition uses datetime.now(UTC)."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    recent_id = _snowflake_at(week_ago)
    old_id = _snowflake_at(long_ago)

    recent, old = partition_by_bulk_delete_window([recent_id, old_id])
    assert recent == [recent_id]
    assert old == [old_id]


def test_partition_splits_at_14_day_cutoff():
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    just_inside = now - timedelta(days=13, hours=23)
    just_outside = now - timedelta(days=14, hours=1)
    very_recent = now - timedelta(minutes=5)
    ancient = now - timedelta(days=400)

    msg_ids = [
        _snowflake_at(very_recent),
        _snowflake_at(just_inside),
        _snowflake_at(just_outside),
        _snowflake_at(ancient),
    ]
    recent, old = partition_by_bulk_delete_window(msg_ids, now=now)

    assert _snowflake_at(very_recent) in recent
    assert _snowflake_at(just_inside) in recent
    assert _snowflake_at(just_outside) in old
    assert _snowflake_at(ancient) in old


def test_partition_empty_input():
    recent, old = partition_by_bulk_delete_window([])
    assert recent == []
    assert old == []


def test_fourteen_days_constant():
    assert FOURTEEN_DAYS == timedelta(days=14)


# ── chunk_for_bulk_delete ──────────────────────────────────────────────


def test_chunk_under_chunk_size_returns_single_chunk():
    assert chunk_for_bulk_delete([1, 2, 3]) == [[1, 2, 3]]


def test_chunk_exact_multiple():
    assert chunk_for_bulk_delete([1, 2, 3, 4], chunk_size=2) == [[1, 2], [3, 4]]


def test_chunk_remainder_goes_in_final_chunk():
    assert chunk_for_bulk_delete([1, 2, 3, 4, 5], chunk_size=2) == [[1, 2], [3, 4], [5]]


def test_chunk_empty_input():
    assert chunk_for_bulk_delete([]) == []


def test_chunk_default_size_is_discord_max():
    """Default chunk size matches Discord's 100-per-call bulk-delete cap."""
    msg_ids = list(range(250))
    chunks = chunk_for_bulk_delete(msg_ids)
    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_chunk_rejects_non_positive_size():
    with pytest.raises(ValueError):
        chunk_for_bulk_delete([1, 2, 3], chunk_size=0)
    with pytest.raises(ValueError):
        chunk_for_bulk_delete([1, 2, 3], chunk_size=-1)


# ── render_progress_bar ────────────────────────────────────────────────


def test_progress_bar_zero():
    bar = render_progress_bar(0, 10, width=10)
    assert bar == "▱▱▱▱▱▱▱▱▱▱ 0/10"


def test_progress_bar_full():
    bar = render_progress_bar(10, 10, width=10)
    assert bar == "▰▰▰▰▰▰▰▰▰▰ 10/10"


def test_progress_bar_half():
    bar = render_progress_bar(5, 10, width=10)
    assert bar == "▰▰▰▰▰▱▱▱▱▱ 5/10"


def test_progress_bar_total_zero_renders_full():
    """Defensive: ``total == 0`` means there was nothing to do, so the bar
    should show "complete" rather than divide-by-zero."""
    bar = render_progress_bar(0, 0, width=4)
    assert bar == "▰▰▰▰ 0/0"


def test_progress_bar_clamps_overshoot():
    """``deleted + failed + replaced`` can momentarily exceed ``total`` due to
    sum semantics — the bar must not produce a negative-padded string."""
    bar = render_progress_bar(15, 10, width=10)
    # Filled clamped to 10, no negative padding
    assert "▰" * 10 in bar
    assert "▱" not in bar
    assert bar.endswith("15/10")


def test_progress_bar_rejects_non_positive_width():
    with pytest.raises(ValueError):
        render_progress_bar(1, 10, width=0)


# ── render_scan_status ─────────────────────────────────────────────────


def test_render_scan_status_format():
    msg = render_scan_status(done=3, total=20, found=42)
    assert "channel **3/20**" in msg
    assert "**42** found" in msg
    assert msg.startswith("Scanning the server")


# ── render_deletion_summary ────────────────────────────────────────────


def test_summary_minimal_reports_deleted_count():
    out = render_deletion_summary(deleted=12, failed=0, replaced=0)
    assert "**12**" in out
    # Nothing about replaced or failed when both are zero
    assert "Forum posts" not in out
    assert "couldn't be deleted" not in out


def test_summary_states_server_side_data_is_retained():
    """The summary must never claim data was deleted — it's kept for moderation."""
    out = render_deletion_summary(deleted=12, failed=0, replaced=0)
    assert "kept for moderation" in out
    # Guard against a regression to the old "cleared / erased" wording.
    assert "cleared" not in out.lower()
    assert "erased" not in out.lower()


def test_summary_noun_tracks_mode():
    out = render_deletion_summary(deleted=3, failed=0, replaced=0, mode=MODE_MEDIA)
    assert "Images & files deleted from Discord: **3**" in out


def test_summary_includes_replaced_when_nonzero():
    out = render_deletion_summary(deleted=5, failed=0, replaced=2)
    assert "Forum posts replaced with tombstone: **2**" in out


def test_summary_includes_failures_when_nonzero():
    out = render_deletion_summary(deleted=5, failed=3, replaced=0)
    assert "couldn't be deleted (no access): **3**" in out


def test_summary_full_kitchen_sink():
    out = render_deletion_summary(deleted=10, failed=2, replaced=1)
    lines = out.splitlines()
    # Header, deleted count, retention line, replaced, failed
    assert len(lines) == 5
    assert lines[0] == "All done. Here's what was removed:"


# ── render_empty_summary ───────────────────────────────────────────────


def test_empty_summary_reports_nothing_found():
    msg = render_empty_summary()
    assert "No messages found" in msg


def test_empty_summary_states_data_untouched():
    msg = render_empty_summary()
    assert "Nothing else was touched" in msg
    assert "cleared" not in msg.lower()
    assert "erased" not in msg.lower()


# ── should_throttle ────────────────────────────────────────────────────


def test_should_throttle_final_update_always_passes():
    """Even if the throttle window hasn't elapsed, the *final* update
    (done >= total) always fires so the user sees completion."""
    assert should_throttle(0.0, 0.5, done=10, total=10, interval=2.0) is False
    assert should_throttle(0.0, 0.0, done=20, total=10, interval=2.0) is False


def test_should_throttle_within_window_skips():
    """An interim update inside the rate-limit window is skipped."""
    assert should_throttle(100.0, 100.5, done=3, total=10, interval=2.0) is True


def test_should_throttle_outside_window_passes():
    """An interim update past the rate-limit window is allowed through."""
    assert should_throttle(100.0, 103.0, done=3, total=10, interval=2.0) is False


def test_should_throttle_boundary_inclusive():
    """``now - last == interval`` exactly: passes (>=, not >)."""
    assert should_throttle(100.0, 102.0, done=3, total=10, interval=2.0) is False
