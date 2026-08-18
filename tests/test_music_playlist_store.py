"""Tests for the music playlist store (migration 165).

Covers ``bot_modules/music_playlist/music_playlist_store.py`` — the window
bookkeeping the rolling-30 design forces: dedupe scoped to the live window
(a rolled-off track CAN come back), trims that return exactly the overflow
oldest-first, the two-people-posted-the-same-song deletion rule, the
idempotent message ledger, one-way review-queue transitions, and the erasure
sweep both tables' ``added_by`` columns need.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.music_playlist.music_playlist_store import (
    BOOKKEEPING_REASONS,
    REASON_MESSAGE_DELETED,
    REASON_ROLLED_OFF,
    RETRYABLE_MESSAGE_STATUSES,
    STATUS_APPROVED,
    STATUS_MESSAGE_GONE,
    STATUS_PROCESSED,
    STATUS_REJECTED,
    STATUS_WRITE_FAILED,
    bump_retry_attempts,
    create_unmatched,
    guilds_with_retryable,
    insert_track,
    is_message_processed,
    latest_review_verdict,
    list_pending,
    list_retryable_messages,
    live_window,
    partition_new_vs_existing,
    purge_member_rows,
    record_duplicate_reference,
    record_message,
    remove_tracks_for_message,
    retire_duplicate_references,
    set_unmatched_status,
    trim_to_window,
)

GUILD = 1400000000000000001
CHANNEL = 1400000000000000002
PLAYLIST = "5AbCdEfPlaylist"
ALICE = 1400000000000000101
BOB = 1400000000000000102
MOD = 1400000000000000103
CAROL = 1400000000000000104


def _add(conn, track_id, message_id, *, added_by=ALICE, added_at=None):
    """Insert a live track with boilerplate filled in."""
    return insert_track(
        conn,
        GUILD,
        playlist_id=PLAYLIST,
        track_id=track_id,
        title=f"Title {track_id}",
        artist=f"Artist {track_id}",
        source_url=f"https://open.spotify.com/track/{track_id}",
        channel_id=CHANNEL,
        message_id=message_id,
        added_by=added_by,
        added_at=added_at,
    )


def _dup(conn, track_id, message_id, *, added_by=BOB):
    return record_duplicate_reference(
        conn,
        GUILD,
        playlist_id=PLAYLIST,
        track_id=track_id,
        title=f"Title {track_id}",
        artist=f"Artist {track_id}",
        source_url=f"https://open.spotify.com/track/{track_id}",
        channel_id=CHANNEL,
        message_id=message_id,
        added_by=added_by,
    )


# ── Message ledger ────────────────────────────────────────────────────


def test_message_ledger_is_idempotent(sync_db_path):
    with open_db(sync_db_path) as conn:
        assert not is_message_processed(conn, GUILD, 555)
        assert record_message(conn, GUILD, 555, CHANNEL) is True
        # Re-scan sees it already recorded — the second write is a no-op.
        assert record_message(conn, GUILD, 555, CHANNEL) is False
        assert is_message_processed(conn, GUILD, 555)
        # Another guild's identical message id is its own entry.
        assert not is_message_processed(conn, GUILD + 1, 555)


@pytest.mark.parametrize(
    "failed_status",
    sorted(RETRYABLE_MESSAGE_STATUSES),
)
def test_failed_message_stays_retryable(sync_db_path, failed_status):
    """A message whose tracks never landed must NOT read as processed.

    Regression: the ledger used to record every outcome as terminal, so a song
    posted while the Spotify grant was read-only — a designed-in state before
    the owner re-consents — was skipped by every later rescan, and reconcile
    could not recover it either (it only pushes tracks the DB already holds).
    """
    with open_db(sync_db_path) as conn:
        assert record_message(
            conn, GUILD, 777, CHANNEL, status=failed_status
        ) is True
        # Seen, but deliberately not "processed" — a rescan must re-fire it.
        assert not is_message_processed(conn, GUILD, 777)

        # The retry lands: the row settles on its real outcome.
        assert record_message(
            conn, GUILD, 777, CHANNEL, status=STATUS_PROCESSED
        ) is True
        assert is_message_processed(conn, GUILD, 777)

        # And a terminal row is never downgraded back to a failure.
        assert record_message(
            conn, GUILD, 777, CHANNEL, status=failed_status
        ) is False
        assert is_message_processed(conn, GUILD, 777)


def _failed(conn, message_id, *, attempts=0, processed_at=1000.0):
    record_message(
        conn, GUILD, message_id, CHANNEL,
        status=STATUS_WRITE_FAILED, processed_at=processed_at,
    )
    for _ in range(attempts):
        bump_retry_attempts(conn, GUILD, message_id)


def _due(conn, *, now, guild=GUILD, limit=10, max_attempts=8):
    return list_retryable_messages(
        conn, guild, base_delay_s=300.0, max_attempts=max_attempts,
        limit=limit, now=now,
    )


def test_retry_backoff_doubles_per_attempt(sync_db_path):
    """Due at processed_at + base * 2^attempts — nothing re-fires early."""
    with open_db(sync_db_path) as conn:
        _failed(conn, 601, attempts=0)   # due at 1000 + 300
        _failed(conn, 602, attempts=2)   # due at 1000 + 1200
        assert _due(conn, now=1200.0) == []
        assert [r["message_id"] for r in _due(conn, now=1300.0)] == [601]
        assert [r["message_id"] for r in _due(conn, now=2200.0)] == [601, 602]


def test_retry_excludes_capped_and_terminal_rows(sync_db_path):
    with open_db(sync_db_path) as conn:
        _failed(conn, 601, attempts=8)
        record_message(conn, GUILD, 602, CHANNEL, status=STATUS_PROCESSED,
                       processed_at=1000.0)
        record_message(conn, GUILD, 603, CHANNEL, status=STATUS_MESSAGE_GONE,
                       processed_at=1000.0)
        assert _due(conn, now=10_000_000.0) == []
        # Capped rows are done for the sweep, but a manual Re-scan still
        # re-fires them — retryable never reads as processed.
        assert not is_message_processed(conn, GUILD, 601)


def test_retry_lists_oldest_first_and_respects_limit(sync_db_path):
    with open_db(sync_db_path) as conn:
        _failed(conn, 601, processed_at=3000.0)
        _failed(conn, 602, processed_at=1000.0)
        _failed(conn, 603, processed_at=2000.0)
        due = _due(conn, now=10_000.0, limit=2)
        assert [r["message_id"] for r in due] == [602, 603]


def test_retry_overwrite_preserves_attempts(sync_db_path):
    """A failed retry re-ledgers the row without resetting its backoff."""
    with open_db(sync_db_path) as conn:
        _failed(conn, 601, attempts=3)
        record_message(conn, GUILD, 601, CHANNEL, status=STATUS_WRITE_FAILED,
                       processed_at=2000.0)
        (row,) = _due(conn, now=2000.0 + 300.0 * 8)
        assert row["attempts"] == 3


def test_message_gone_is_terminal(sync_db_path):
    with open_db(sync_db_path) as conn:
        _failed(conn, 601)
        assert record_message(
            conn, GUILD, 601, CHANNEL, status=STATUS_MESSAGE_GONE
        ) is True
        assert is_message_processed(conn, GUILD, 601)


def test_guilds_with_retryable_rows(sync_db_path):
    with open_db(sync_db_path) as conn:
        _failed(conn, 601)
        record_message(conn, GUILD + 1, 602, CHANNEL, status=STATUS_PROCESSED)
        # A second guild whose only retryable row is past the attempt cap
        # doesn't earn a sweep visit.
        record_message(conn, GUILD + 2, 603, CHANNEL,
                       status=STATUS_WRITE_FAILED)
        for _ in range(8):
            bump_retry_attempts(conn, GUILD + 2, 603)
        assert guilds_with_retryable(conn, max_attempts=8) == [GUILD]


# ── Window + dedupe ───────────────────────────────────────────────────


def test_live_window_newest_first(sync_db_path):
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1)
        _add(conn, "t2", 2)
        _add(conn, "t3", 3)
        assert [r["track_id"] for r in live_window(conn, GUILD, PLAYLIST)] == [
            "t3", "t2", "t1",
        ]


def test_partition_checks_live_window_only(sync_db_path):
    with open_db(sync_db_path) as conn:
        _add(conn, "old", 1)
        _add(conn, "live", 2)
        # "old" rolls off — history now, not window.
        trim_to_window(conn, GUILD, PLAYLIST, 1)
        new, existing = partition_new_vs_existing(
            conn, GUILD, PLAYLIST, ["live", "old", "fresh"]
        )
        assert new == ["old", "fresh"]
        assert existing == ["live"]


def test_partition_dedupes_within_the_input(sync_db_path):
    # Two links to the same song in one message: added once.
    with open_db(sync_db_path) as conn:
        new, existing = partition_new_vs_existing(
            conn, GUILD, PLAYLIST, ["t1", "t1"]
        )
        assert new == ["t1"]
        assert existing == ["t1"]


def test_rolled_off_track_can_be_readded(sync_db_path):
    # The partial unique index blocks live duplicates but not history rows.
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1)
        with pytest.raises(sqlite3.IntegrityError):
            _add(conn, "t1", 2)
        trim_to_window(conn, GUILD, PLAYLIST, 0)
        _add(conn, "t1", 3)  # re-add after roll-off: no conflict
        window = live_window(conn, GUILD, PLAYLIST)
        assert [r["track_id"] for r in window] == ["t1"]
        assert window[0]["message_id"] == 3


# ── Trim ──────────────────────────────────────────────────────────────


def test_trim_returns_exactly_the_overflow_oldest_first(sync_db_path):
    with open_db(sync_db_path) as conn:
        for i in range(1, 6):
            _add(conn, f"t{i}", i)
        rolled = trim_to_window(conn, GUILD, PLAYLIST, 3)
        assert [r["track_id"] for r in rolled] == ["t1", "t2"]
        assert [r["track_id"] for r in live_window(conn, GUILD, PLAYLIST)] == [
            "t5", "t4", "t3",
        ]
        # History rows carry the reason, not a bare tombstone.
        reasons = {
            r["removal_reason"]
            for r in conn.execute(
                "SELECT removal_reason FROM music_playlist_tracks "
                "WHERE removed_at IS NOT NULL"
            )
        }
        assert reasons == {REASON_ROLLED_OFF}


def test_trim_under_window_is_a_noop(sync_db_path):
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1)
        assert trim_to_window(conn, GUILD, PLAYLIST, 30) == []
        assert len(live_window(conn, GUILD, PLAYLIST)) == 1


# ── Deletion + the shared-song rule ───────────────────────────────────


def test_delete_sole_reference_removes_the_track(sync_db_path):
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1)
        removed = remove_tracks_for_message(conn, GUILD, 1)
        assert [r["track_id"] for r in removed] == ["t1"]
        assert live_window(conn, GUILD, PLAYLIST) == []
        row = conn.execute(
            "SELECT removal_reason FROM music_playlist_tracks"
        ).fetchone()
        assert row["removal_reason"] == REASON_MESSAGE_DELETED


def test_delete_keeps_track_another_live_message_references(sync_db_path):
    # Alice posts t1, Bob posts it again (duplicate). Alice deletes hers:
    # the track stays, now attributed to Bob's message.
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1, added_by=ALICE)
        _dup(conn, "t1", 2, added_by=BOB)
        removed = remove_tracks_for_message(conn, GUILD, 1)
        assert removed == []  # nothing to take off Spotify
        window = live_window(conn, GUILD, PLAYLIST)
        assert [r["track_id"] for r in window] == ["t1"]
        assert window[0]["message_id"] == 2
        assert window[0]["added_by"] == BOB
        # Bob's deletion is now the last reference — the track goes.
        removed = remove_tracks_for_message(conn, GUILD, 2)
        assert [r["track_id"] for r in removed] == ["t1"]
        assert live_window(conn, GUILD, PLAYLIST) == []


def test_deleted_duplicate_cannot_rescue_a_later_deletion(sync_db_path):
    # Bob deletes his duplicate post first; when Alice then deletes the
    # original, no live message references t1 and it must be removed.
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1, added_by=ALICE)
        _dup(conn, "t1", 2, added_by=BOB)
        assert remove_tracks_for_message(conn, GUILD, 2) == []
        assert len(live_window(conn, GUILD, PLAYLIST)) == 1  # still live
        removed = remove_tracks_for_message(conn, GUILD, 1)
        assert [r["track_id"] for r in removed] == ["t1"]
        assert live_window(conn, GUILD, PLAYLIST) == []


def test_stale_reference_cannot_resurrect_a_later_cycle(sync_db_path):
    """A duplicate reference dies with the tenure it referenced.

    Regression: duplicate rows were never retired, so one left over from an
    earlier cycle resurrected a track in a later one — as a live row attributed
    to a message that contributed nothing to it, and one the caller never got
    told to remove from Spotify.
    """
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1, added_by=ALICE)      # Alice posts it
        _dup(conn, "t1", 2, added_by=BOB)        # Bob posts it again
        # The window turns over: t1 rolls off, taking Bob's reference with it.
        trim_to_window(conn, GUILD, PLAYLIST, 0)
        assert live_window(conn, GUILD, PLAYLIST) == []

        # New cycle: Carol posts t1, then deletes her message.
        _add(conn, "t1", 3, added_by=CAROL)
        removed = remove_tracks_for_message(conn, GUILD, 3)
        assert [r["track_id"] for r in removed] == ["t1"]
        assert live_window(conn, GUILD, PLAYLIST) == []


def test_admin_removal_retires_outstanding_references(sync_db_path):
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1, added_by=ALICE)
        _dup(conn, "t1", 2, added_by=BOB)
        row = live_window(conn, GUILD, PLAYLIST)[0]
        # What the admin-removal route does to the live row.
        conn.execute(
            "UPDATE music_playlist_tracks SET removed_at = ?, "
            "removal_reason = ? WHERE id = ?",
            (1000.0, "admin", row["id"]),
        )
        retire_duplicate_references(
            conn, GUILD, playlist_id=PLAYLIST, track_id="t1", removed_at=1000.0
        )
        # Bob's reference can no longer put the track back.
        _add(conn, "t1", 3, added_by=CAROL)
        removed = remove_tracks_for_message(conn, GUILD, 3)
        assert [r["track_id"] for r in removed] == ["t1"]
        assert live_window(conn, GUILD, PLAYLIST) == []


def test_duplicate_reference_is_idempotent_per_message(sync_db_path):
    """Re-scanning a retryable message must not stack phantom references."""
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1, added_by=ALICE)
        assert _dup(conn, "t1", 2, added_by=BOB) > 0
        for _ in range(3):  # three Re-scan sweeps over the same message
            assert _dup(conn, "t1", 2, added_by=BOB) == 0
        refs = conn.execute(
            "SELECT COUNT(*) c FROM music_playlist_tracks "
            "WHERE message_id = ? AND track_id = ?",
            (2, "t1"),
        ).fetchone()
        assert refs["c"] == 1


def test_deleting_a_duplicate_does_not_forge_history(sync_db_path):
    """A reference never occupied the window, so it is not history.

    Regression: retiring it as ``message_deleted`` listed the track in History
    as removed while it was still sitting in the Live Window.
    """
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1, added_by=ALICE)
        _dup(conn, "t1", 2, added_by=BOB)
        assert remove_tracks_for_message(conn, GUILD, 2) == []
        assert [r["track_id"] for r in live_window(conn, GUILD, PLAYLIST)] == [
            "t1",
        ]
        reasons = {
            r["removal_reason"]
            for r in conn.execute(
                "SELECT removal_reason FROM music_playlist_tracks "
                "WHERE removed_at IS NOT NULL"
            ).fetchall()
        }
        # Nothing claims the track left the playlist.
        assert REASON_MESSAGE_DELETED not in reasons
        assert reasons <= BOOKKEEPING_REASONS


def test_delete_only_touches_that_messages_tracks(sync_db_path):
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1)
        _add(conn, "t2", 2)
        remove_tracks_for_message(conn, GUILD, 1)
        assert [r["track_id"] for r in live_window(conn, GUILD, PLAYLIST)] == [
            "t2",
        ]


# ── Unmatched review queue ────────────────────────────────────────────


def _queue(conn, message_id=9, *, added_by=ALICE):
    return create_unmatched(
        conn,
        GUILD,
        channel_id=CHANNEL,
        message_id=message_id,
        source_url="https://youtu.be/dQw4w9WgXcQ",
        added_by=added_by,
        extracted_title="Some Song (Official Video)",
        extracted_channel="SomeArtist - Topic",
        candidate_track_id="cand1",
        candidate_name="Some Song",
        candidate_artist="Some Artist",
        confidence=0.61,
        reason="below threshold",
    )


def test_latest_review_verdict_ignores_pending_and_takes_newest(sync_db_path):
    url = "https://youtu.be/dQw4w9WgXcQ"
    with open_db(sync_db_path) as conn:
        first = _queue(conn, message_id=9)
        second = _queue(conn, message_id=10)
        _queue(conn, message_id=11)  # stays pending — an open question
        set_unmatched_status(conn, GUILD, first, STATUS_APPROVED,
                             reviewed_by=MOD, reviewed_at=100.0)
        set_unmatched_status(conn, GUILD, second, STATUS_REJECTED,
                             reviewed_by=MOD, reviewed_at=200.0)
        verdict = latest_review_verdict(conn, GUILD, url)
        assert verdict is not None and verdict["status"] == STATUS_REJECTED
        # Unknown link, or another guild's identical one: no verdict.
        assert latest_review_verdict(conn, GUILD, "https://youtu.be/other") is None
        assert latest_review_verdict(conn, GUILD + 1, url) is None


def test_unmatched_pending_then_resolved(sync_db_path):
    with open_db(sync_db_path) as conn:
        first = _queue(conn, 9)
        second = _queue(conn, 10)
        assert [r["id"] for r in list_pending(conn, GUILD)] == [first, second]
        assert set_unmatched_status(
            conn, GUILD, first, STATUS_APPROVED, reviewed_by=MOD
        )
        assert [r["id"] for r in list_pending(conn, GUILD)] == [second]
        row = conn.execute(
            "SELECT status, reviewed_by, reviewed_at "
            "FROM music_playlist_unmatched WHERE id = ?",
            (first,),
        ).fetchone()
        assert row["status"] == STATUS_APPROVED
        assert row["reviewed_by"] == MOD
        assert row["reviewed_at"] is not None


@pytest.mark.parametrize("verdict", [STATUS_APPROVED, STATUS_REJECTED])
def test_unmatched_resolves_exactly_once(sync_db_path, verdict):
    # Two reviewers racing: the second write loses instead of overwriting.
    with open_db(sync_db_path) as conn:
        item = _queue(conn)
        assert set_unmatched_status(conn, GUILD, item, verdict, reviewed_by=MOD)
        assert not set_unmatched_status(
            conn, GUILD, item, STATUS_REJECTED, reviewed_by=MOD + 1
        )
        row = conn.execute(
            "SELECT status, reviewed_by FROM music_playlist_unmatched "
            "WHERE id = ?",
            (item,),
        ).fetchone()
        assert row["status"] == verdict
        assert row["reviewed_by"] == MOD


def test_unmatched_guard_rails(sync_db_path):
    with open_db(sync_db_path) as conn:
        item = _queue(conn)
        with pytest.raises(ValueError):
            set_unmatched_status(conn, GUILD, item, "pending", reviewed_by=MOD)
        # Another guild can't resolve this guild's item by id.
        assert not set_unmatched_status(
            conn, GUILD + 1, item, STATUS_APPROVED, reviewed_by=MOD
        )
        assert len(list_pending(conn, GUILD)) == 1


# ── Erasure ───────────────────────────────────────────────────────────


def test_purge_member_rows_clears_both_tables(sync_db_path):
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1, added_by=ALICE)
        _add(conn, "t2", 2, added_by=BOB)
        _dup(conn, "t2", 3, added_by=ALICE)
        alice_item = _queue(conn, 4, added_by=ALICE)
        bob_item = _queue(conn, 5, added_by=BOB)
        # Alice also reviewed Bob's item — the review survives, unattributed.
        set_unmatched_status(
            conn, GUILD, bob_item, STATUS_REJECTED, reviewed_by=ALICE
        )

        assert purge_member_rows(conn, GUILD, ALICE) == 3

        tracks = conn.execute(
            "SELECT added_by FROM music_playlist_tracks"
        ).fetchall()
        assert [r["added_by"] for r in tracks] == [BOB]
        items = conn.execute(
            "SELECT id, added_by, reviewed_by FROM music_playlist_unmatched"
        ).fetchall()
        assert [r["id"] for r in items] == [bob_item]
        assert items[0]["reviewed_by"] is None
        assert alice_item not in [r["id"] for r in items]


def test_purge_member_rows_is_guild_scoped(sync_db_path):
    with open_db(sync_db_path) as conn:
        _add(conn, "t1", 1, added_by=ALICE)
        assert purge_member_rows(conn, GUILD + 1, ALICE) == 0
        assert len(live_window(conn, GUILD, PLAYLIST)) == 1
