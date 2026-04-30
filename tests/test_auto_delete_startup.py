"""Integration tests for auto-delete startup catch-up.

Covers the two startup-perf changes:
- ``_scan_and_delete_channel_history`` forwards ``after`` to ``channel.history``.
- ``run_startup_auto_delete`` processes channels in parallel, gated by
  ``AUTO_DELETE_SETTINGS.startup_concurrency``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from db_utils import open_db
from services import auto_delete_service
from services.auto_delete_service import (
    _scan_and_delete_channel_history,
    init_auto_delete_tables,
    pop_due_auto_delete_message_ids,
    run_startup_auto_delete,
    upsert_auto_delete_rule,
)


# ── _scan_and_delete_channel_history forwards `after` ─────────────────


class _RecordingChannel:
    """Minimal stand-in for a TextChannel whose history yields nothing.

    Records the kwargs of the most recent ``history()`` call so tests can
    assert on the bounded-scan parameters.
    """

    def __init__(self) -> None:
        self.id = 1234
        self.name = "test-channel"
        self.history_kwargs: dict[str, Any] | None = None

    def history(self, **kwargs: Any) -> Any:
        self.history_kwargs = kwargs

        async def _empty():
            for _ in ():
                yield _

        return _empty()


@pytest.mark.asyncio
async def test_scan_forwards_after_when_provided():
    channel = _RecordingChannel()
    cutoff = datetime(2026, 4, 29, tzinfo=timezone.utc)
    after = cutoff - timedelta(days=2)

    deleted, failed = await _scan_and_delete_channel_history(
        channel, cutoff, reason="test", after=after  # type: ignore[arg-type]
    )

    assert deleted == 0 and failed == 0
    assert channel.history_kwargs is not None
    assert channel.history_kwargs["after"] == after
    assert channel.history_kwargs["before"] == cutoff
    assert channel.history_kwargs["oldest_first"] is True


@pytest.mark.asyncio
async def test_scan_omits_after_when_none():
    channel = _RecordingChannel()
    cutoff = datetime(2026, 4, 29, tzinfo=timezone.utc)

    await _scan_and_delete_channel_history(
        channel, cutoff, reason="test"  # type: ignore[arg-type]
    )

    assert channel.history_kwargs is not None
    # No `after` key means we walk the entire channel history (the only safe
    # behavior when we have no prior-run state).
    assert "after" not in channel.history_kwargs


@pytest.mark.asyncio
async def test_scan_drops_before_cutoff_when_tracking_enabled(tmp_path):
    """When db_path is provided, the walk must NOT cap at cutoff — we need
    to read past it so younger messages can be tracked for the tick path."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)

    channel = _RecordingChannel()
    cutoff = datetime(2026, 4, 29, tzinfo=timezone.utc)

    await _scan_and_delete_channel_history(
        channel,  # type: ignore[arg-type]
        cutoff,
        reason="test",
        db_path=db_path,
        guild_id=1,
    )

    assert channel.history_kwargs is not None
    assert "before" not in channel.history_kwargs


# ── _scan_and_delete_channel_history tracks downtime orphans ──────────


class _FakeMessage:
    def __init__(self, message_id: int, created_at: datetime, *, pinned: bool = False):
        self.id = message_id
        self.created_at = created_at
        self.pinned = pinned


class _FakeChannel:
    """Channel that yields a scripted message list and records deletes."""

    def __init__(self, messages: list[_FakeMessage]):
        self.id = 999
        self.name = "fake"
        self._messages = messages
        self.bulk_deleted: list[list[int]] = []

    def history(self, **kwargs: Any) -> Any:
        before: datetime | None = kwargs.get("before")
        after: datetime | None = kwargs.get("after")

        async def _iter():
            for m in self._messages:
                if before is not None and m.created_at >= before:
                    continue
                if after is not None and m.created_at <= after:
                    continue
                yield m

        return _iter()

    def get_partial_message(self, message_id: int):
        return _FakeMessage(message_id, datetime.now(timezone.utc))

    async def delete_messages(self, partials, *, reason: str):
        del reason
        self.bulk_deleted.append([p.id for p in partials])


@pytest.mark.asyncio
async def test_scan_tracks_messages_younger_than_cutoff(tmp_path):
    """Regression: messages posted during bot downtime aren't tracked by
    on_message, so the startup scan must insert them into auto_delete_messages
    or they become permanent orphans (the tick path queries that table only)."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)

    cutoff = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
    # One eligible message (older than cutoff) + one downtime orphan (younger
    # than cutoff but unpinned and untracked).
    eligible = _FakeMessage(1001, cutoff - timedelta(hours=1))  # delete now
    orphan = _FakeMessage(1002, cutoff + timedelta(minutes=30))  # track for later
    channel = _FakeChannel([eligible, orphan])

    await _scan_and_delete_channel_history(
        channel,  # type: ignore[arg-type]
        cutoff,
        reason="test",
        db_path=db_path,
        guild_id=42,
    )

    # The eligible one was bulk-deleted.
    assert channel.bulk_deleted == [[1001]]

    # The orphan is now tracked, so the next tick will age it out.
    with open_db(db_path) as conn:
        tracked = pop_due_auto_delete_message_ids(
            conn, guild_id=42, channel_id=999, cutoff_ts=cutoff.timestamp() + 86400
        )
    assert [mid for mid, _ in tracked] == [1002]


@pytest.mark.asyncio
async def test_scan_does_not_track_when_db_path_omitted(tmp_path):
    """Backward-compat: with no db_path, behave like the old delete-only scan."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)

    cutoff = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
    eligible = _FakeMessage(1001, cutoff - timedelta(hours=1))
    young = _FakeMessage(1002, cutoff + timedelta(minutes=30))
    channel = _FakeChannel([eligible, young])

    await _scan_and_delete_channel_history(
        channel, cutoff, reason="test"  # type: ignore[arg-type]
    )

    # Old contract: only the eligible one gets touched, nothing tracked.
    assert channel.bulk_deleted == [[1001]]
    with open_db(db_path) as conn:
        tracked = pop_due_auto_delete_message_ids(
            conn, guild_id=42, channel_id=999, cutoff_ts=cutoff.timestamp() + 86400
        )
    assert tracked == []


@pytest.mark.asyncio
async def test_scan_skips_pinned_messages_when_tracking(tmp_path):
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)

    cutoff = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
    pinned_young = _FakeMessage(1001, cutoff + timedelta(hours=1), pinned=True)
    pinned_old = _FakeMessage(1002, cutoff - timedelta(hours=1), pinned=True)
    channel = _FakeChannel([pinned_young, pinned_old])

    await _scan_and_delete_channel_history(
        channel,  # type: ignore[arg-type]
        cutoff,
        reason="test",
        db_path=db_path,
        guild_id=42,
    )

    assert channel.bulk_deleted == []
    with open_db(db_path) as conn:
        tracked = pop_due_auto_delete_message_ids(
            conn, guild_id=42, channel_id=999, cutoff_ts=cutoff.timestamp() + 86400
        )
    assert tracked == []


# ── run_startup_auto_delete runs channels in parallel ─────────────────


@pytest.mark.asyncio
async def test_startup_processes_channels_in_parallel(tmp_path, monkeypatch):
    """Three rules with a 0.2s per-rule scan must finish in well under 3×0.2s."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)
    upsert_auto_delete_rule(db_path, 1, 100, 86400, 3600)
    upsert_auto_delete_rule(db_path, 1, 200, 86400, 3600)
    upsert_auto_delete_rule(db_path, 1, 300, 86400, 3600)

    per_rule_seconds = 0.2

    in_flight = 0
    peak_in_flight = 0
    lock = asyncio.Lock()

    async def fake_run_for_rule(_bot, _db, _rule, _now_ts, semaphore):
        nonlocal in_flight, peak_in_flight
        async with semaphore:
            async with lock:
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
            try:
                await asyncio.sleep(per_rule_seconds)
            finally:
                async with lock:
                    in_flight -= 1

    monkeypatch.setattr(auto_delete_service, "_run_startup_for_rule", fake_run_for_rule)

    start = time.monotonic()
    await run_startup_auto_delete(bot=object(), db_path=db_path)  # type: ignore[arg-type]
    elapsed = time.monotonic() - start

    # All three should have been in flight at once (concurrency >= 3 by default).
    assert peak_in_flight == 3
    # Serial would be ~0.6s; concurrent should be ~0.2s plus scheduling overhead.
    assert elapsed < per_rule_seconds * 2, f"startup ran serially ({elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_startup_respects_concurrency_cap(tmp_path, monkeypatch):
    """When concurrency cap is 2, only 2 rules can be in flight at once."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)
    for cid in (100, 200, 300, 400):
        upsert_auto_delete_rule(db_path, 1, cid, 86400, 3600)

    capped_settings = dataclasses.replace(
        auto_delete_service.AUTO_DELETE_SETTINGS, startup_concurrency=2
    )
    monkeypatch.setattr(auto_delete_service, "AUTO_DELETE_SETTINGS", capped_settings)

    in_flight = 0
    peak_in_flight = 0
    lock = asyncio.Lock()

    async def fake_run_for_rule(_bot, _db, _rule, _now_ts, semaphore):
        nonlocal in_flight, peak_in_flight
        async with semaphore:
            async with lock:
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
            try:
                await asyncio.sleep(0.05)
            finally:
                async with lock:
                    in_flight -= 1

    monkeypatch.setattr(auto_delete_service, "_run_startup_for_rule", fake_run_for_rule)

    await run_startup_auto_delete(bot=object(), db_path=db_path)  # type: ignore[arg-type]

    assert peak_in_flight == 2


@pytest.mark.asyncio
async def test_startup_no_rules_is_noop(tmp_path):
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)

    # Should return cleanly without touching the bot.
    await run_startup_auto_delete(bot=object(), db_path=db_path)  # type: ignore[arg-type]


# ── startup scan preserves last_run_ts when rule isn't overdue ────────


class _StubBot:
    """Minimal bot stub. The guild it returns is opaque since the
    real channel resolution is monkeypatched out."""

    def __init__(self, guild_id: int) -> None:
        self._guild_id = guild_id

    def get_guild(self, guild_id: int):
        if guild_id != self._guild_id:
            return None

        class _Guild:
            name = "stub-guild"
            id = guild_id

        return _Guild()


async def _run_startup_against_stub_channel(
    db_path: Any,
    guild_id: int,
    channel_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive run_startup_auto_delete with a minimal channel + bot stub."""
    channel = _FakeChannel([])
    channel.id = channel_id  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "utils.get_guild_channel_or_thread",
        lambda _guild, cid: channel if cid == channel_id else None,
    )
    bot = _StubBot(guild_id)
    await run_startup_auto_delete(bot=bot, db_path=db_path)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_startup_preserves_last_run_when_not_overdue(tmp_path, monkeypatch):
    """Regression: a restart 5 min after the previous tick must not push the
    next tick out by a full interval. Only update last_run_ts when the rule was
    already overdue at boot."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)

    guild_id = 1
    channel_id = 999
    interval = 3600
    max_age = 3600

    recent_run_ts = time.time() - 300  # 5 min ago, well within the interval

    upsert_auto_delete_rule(
        db_path,
        guild_id,
        channel_id,
        max_age,
        interval,
        last_run_ts=recent_run_ts,
    )

    await _run_startup_against_stub_channel(db_path, guild_id, channel_id, monkeypatch)

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT last_run_ts FROM auto_delete_rules WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
    assert row["last_run_ts"] == pytest.approx(recent_run_ts), (
        "Startup scan must not reset last_run_ts when the rule isn't overdue"
    )


@pytest.mark.asyncio
async def test_startup_advances_last_run_when_overdue(tmp_path, monkeypatch):
    """When the rule was overdue at boot, the startup scan acts as that tick
    and last_run_ts must advance to the boot timestamp."""
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)

    guild_id = 1
    channel_id = 999
    interval = 3600
    max_age = 3600

    overdue_run_ts = time.time() - 7200  # 2h ago, well past the 1h interval

    upsert_auto_delete_rule(
        db_path,
        guild_id,
        channel_id,
        max_age,
        interval,
        last_run_ts=overdue_run_ts,
    )

    await _run_startup_against_stub_channel(db_path, guild_id, channel_id, monkeypatch)

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT last_run_ts FROM auto_delete_rules WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
    assert row["last_run_ts"] > overdue_run_ts + interval, (
        "Startup scan must advance last_run_ts when the rule was overdue"
    )
