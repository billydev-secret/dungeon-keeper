"""Storage for the music playlist window (migration 164).

The rolling-window bookkeeping — what's live, what rolled off, who posted
what — with no Spotify calls and no opinions about matching. The pipeline
(``music_playlist_service``) decides *what* to add or remove; this module
records it and answers the two questions the window forces:

* **Dedupe is scoped to the live window, not all time.** The partial unique
  index (one live row per guild/platform/playlist/track) is the rule; a
  rolled-off track re-adds cleanly because its history row is outside the
  index.
* **Two people can post the same song, and the playlist holds it once** —
  so a duplicate post is recorded (:func:`record_duplicate_reference`, a row
  born with ``removal_reason='duplicate'``), and deleting a message removes
  a track only when no other live message still references it. When one
  does, the surviving reference is resurrected as the live row instead.

Everything here is sync, takes a ``sqlite3.Connection``, and leaves the
transaction to the caller, the way ``mention_awards/store.py`` does.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Mapping, Sequence

PLATFORM_SPOTIFY = "spotify"

# removal_reason values. 'duplicate' rows are references, not removals — they
# are written at insert time and only ever read by the deletion path.
REASON_ROLLED_OFF = "rolled_off"
REASON_MESSAGE_DELETED = "message_deleted"
REASON_DUPLICATE = "duplicate"
REASON_ADMIN = "admin"
# A duplicate reference that outlived the live row it pointed at. A
# 'duplicate' row is a claim on the live row's *tenure in the window*, not on
# the track forever: once that tenure ends (rolled off, admin-removed, or the
# last message holding it deleted), any remaining references are stale and must
# stop counting, or a later cycle resurrects one as a phantom live row
# attributed to a message that contributed nothing to it.
REASON_STALE_REFERENCE = "stale_reference"

# Reasons that mark bookkeeping rows rather than real playlist history. The
# dashboard's History view filters these out — they never occupied the window.
BOOKKEEPING_REASONS = frozenset({REASON_DUPLICATE, REASON_STALE_REFERENCE})

# Unmatched review-queue statuses. pending → approved/rejected, one way.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
REVIEW_STATUSES = frozenset({STATUS_APPROVED, STATUS_REJECTED})

# Processed-message ledger statuses.
STATUS_PROCESSED = "processed"
STATUS_NO_LINKS = "no_links"
STATUS_WRITE_FAILED = "write_failed"
STATUS_RESOLVE_FAILED = "resolve_failed"
# The retry sweep found the message deleted from Discord. Terminal: there is
# nothing left to re-process, and without it a deleted message's retryable
# row would be re-fetched forever.
STATUS_MESSAGE_GONE = "message_gone"

# Statuses that mean "we saw this message but did NOT land its tracks" —
# a Spotify write that was refused (read-only grant, 403, exhausted 429s) or a
# resolve that errored out. These are deliberately NOT terminal: the ledger
# exists for idempotency, and treating a failure as processed turns a transient
# outage into permanent silent data loss. The window before the bot owner
# re-consents is a *designed-in* read-only state, so every song posted in it
# would otherwise be dropped with no way back — reconcile can't help, because
# it only pushes tracks the DB already holds. A rescan re-fires these instead.
RETRYABLE_MESSAGE_STATUSES = frozenset(
    {STATUS_WRITE_FAILED, STATUS_RESOLVE_FAILED}
)
# Bound into the ledger's SQL below. Built once, and the parameters are always
# passed as ``sorted(RETRYABLE_MESSAGE_STATUSES)`` so placeholders and values
# stay in step if the set ever grows.
_RETRYABLE_PLACEHOLDERS = ", ".join("?" * len(RETRYABLE_MESSAGE_STATUSES))


# ── Processed-message ledger ──────────────────────────────────────────


def record_message(
    conn: sqlite3.Connection,
    guild_id: int,
    message_id: int,
    channel_id: int,
    *,
    status: str = STATUS_PROCESSED,
    processed_at: float | None = None,
) -> bool:
    """Mark a message as seen. False if it was already terminally ledgered.

    The (guild, message) primary key is the idempotency story: a restart or a
    manual re-scan calls this first and skips the message when it returns
    False. A row in a retryable status (see ``RETRYABLE_MESSAGE_STATUSES``) is
    *overwritten* rather than ignored, so a message that failed to land its
    tracks can be re-processed and settle on its real outcome. Terminal rows
    are never downgraded back to a failure.
    """
    cur = conn.execute(
        "INSERT INTO music_playlist_messages "
        "(guild_id, message_id, channel_id, status, processed_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, message_id) DO UPDATE SET "
        "  status = excluded.status, "
        "  channel_id = excluded.channel_id, "
        "  processed_at = excluded.processed_at "
        f"WHERE music_playlist_messages.status IN ({_RETRYABLE_PLACEHOLDERS})",
        (guild_id, message_id, channel_id, status,
         time.time() if processed_at is None else processed_at,
         *sorted(RETRYABLE_MESSAGE_STATUSES)),
    )
    return cur.rowcount > 0


def list_retryable_messages(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    base_delay_s: float,
    max_attempts: int,
    limit: int,
    now: float | None = None,
) -> list[Mapping[str, Any]]:
    """Ledger rows due for an automatic retry, oldest failure first.

    A row is due when its exponential-backoff window has passed:
    ``processed_at + base_delay_s * 2^attempts <= now`` (the upsert refreshes
    ``processed_at`` on every failed retry, so the clock runs from the latest
    failure). Rows at or past *max_attempts* never come back from here — they
    stay retryable for a manual Re-scan, which ignores attempts entirely.
    """
    ts = time.time() if now is None else now
    return conn.execute(
        "SELECT * FROM music_playlist_messages "
        f"WHERE guild_id = ? AND status IN ({_RETRYABLE_PLACEHOLDERS}) "
        "AND attempts < ? AND processed_at + ? * (1 << attempts) <= ? "
        "ORDER BY processed_at LIMIT ?",
        (guild_id, *sorted(RETRYABLE_MESSAGE_STATUSES),
         max_attempts, base_delay_s, ts, limit),
    ).fetchall()


def bump_retry_attempts(
    conn: sqlite3.Connection, guild_id: int, message_id: int
) -> None:
    """Count a retry against a ledger row (call before re-processing it)."""
    conn.execute(
        "UPDATE music_playlist_messages SET attempts = attempts + 1 "
        "WHERE guild_id = ? AND message_id = ?",
        (guild_id, message_id),
    )


def guilds_with_retryable(
    conn: sqlite3.Connection, *, max_attempts: int
) -> list[int]:
    """Guilds holding at least one row the retry sweep could still act on."""
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT guild_id FROM music_playlist_messages "
            f"WHERE status IN ({_RETRYABLE_PLACEHOLDERS}) AND attempts < ?",
            (*sorted(RETRYABLE_MESSAGE_STATUSES), max_attempts),
        ).fetchall()
    ]


def is_message_processed(
    conn: sqlite3.Connection, guild_id: int, message_id: int
) -> bool:
    """True only for a *terminal* ledger row.

    A retryable row reads as unprocessed on purpose — that is what lets a
    rescan pick up messages whose tracks never landed.
    """
    row = conn.execute(
        "SELECT 1 FROM music_playlist_messages "
        "WHERE guild_id = ? AND message_id = ? "
        f"AND status NOT IN ({_RETRYABLE_PLACEHOLDERS})",
        (guild_id, message_id, *sorted(RETRYABLE_MESSAGE_STATUSES)),
    ).fetchone()
    return row is not None


# ── The window ────────────────────────────────────────────────────────


def live_window(
    conn: sqlite3.Connection,
    guild_id: int,
    playlist_id: str,
    *,
    platform: str = PLATFORM_SPOTIFY,
) -> list[Mapping[str, Any]]:
    """The live playlist rows, newest first (insertion order, not clock)."""
    return conn.execute(
        "SELECT * FROM music_playlist_tracks "
        "WHERE guild_id = ? AND platform = ? AND playlist_id = ? "
        "AND removed_at IS NULL ORDER BY id DESC",
        (guild_id, platform, playlist_id),
    ).fetchall()


def insert_track(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    playlist_id: str,
    track_id: str,
    title: str,
    artist: str,
    source_url: str | None,
    channel_id: int,
    message_id: int,
    added_by: int,
    platform: str = PLATFORM_SPOTIFY,
    added_at: float | None = None,
) -> int:
    """Insert a live window row, returning its id.

    Callers gate on :func:`partition_new_vs_existing` first; inserting a
    track that is already live violates the partial unique index and raises
    ``sqlite3.IntegrityError`` — a caller bug, deliberately not swallowed.
    """
    cur = conn.execute(
        "INSERT INTO music_playlist_tracks "
        "(guild_id, platform, playlist_id, track_id, title, artist, "
        " source_url, channel_id, message_id, added_by, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, platform, playlist_id, track_id, title, artist,
         source_url, channel_id, message_id, added_by,
         time.time() if added_at is None else added_at),
    )
    return int(cur.lastrowid or 0)


def record_duplicate_reference(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    playlist_id: str,
    track_id: str,
    title: str,
    artist: str,
    source_url: str | None,
    channel_id: int,
    message_id: int,
    added_by: int,
    platform: str = PLATFORM_SPOTIFY,
    added_at: float | None = None,
) -> int:
    """Record that a message posted a track the window already holds.

    The row is born removed (``removal_reason='duplicate'``) so it never
    collides with the live row's unique index — it exists purely so
    :func:`remove_tracks_for_message` can tell "last reference gone, remove
    the track" from "someone else also posted this, keep it". The service
    calls this for every track :func:`partition_new_vs_existing` reports as
    existing (the 🔁 reaction case).

    Idempotent per ``(message_id, track_id)``: returns 0 without inserting when
    this message already references this track. Messages in a retryable ledger
    status are re-processed by every Re-scan, so without the guard a single
    message accumulates a phantom reference per sweep — and each one can later
    resurrect the track.
    """
    existing = conn.execute(
        "SELECT id FROM music_playlist_tracks "
        "WHERE guild_id = ? AND platform = ? AND playlist_id = ? "
        "AND track_id = ? AND message_id = ? LIMIT 1",
        (guild_id, platform, playlist_id, track_id, message_id),
    ).fetchone()
    if existing is not None:
        return 0
    ts = time.time() if added_at is None else added_at
    cur = conn.execute(
        "INSERT INTO music_playlist_tracks "
        "(guild_id, platform, playlist_id, track_id, title, artist, "
        " source_url, channel_id, message_id, added_by, added_at, "
        " removed_at, removal_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, platform, playlist_id, track_id, title, artist,
         source_url, channel_id, message_id, added_by, ts, ts,
         REASON_DUPLICATE),
    )
    return int(cur.lastrowid or 0)


def partition_new_vs_existing(
    conn: sqlite3.Connection,
    guild_id: int,
    playlist_id: str,
    track_ids: Sequence[str],
    *,
    platform: str = PLATFORM_SPOTIFY,
) -> tuple[list[str], list[str]]:
    """Split track ids into (new, existing) against the LIVE window only.

    History rows don't count — a track that rolled off is new again. Input
    order is preserved, and a track repeated *within* the input counts as
    new once and existing after (two links to the same song in one message
    add it once).
    """
    if not track_ids:
        return [], []
    unique = list(dict.fromkeys(track_ids))
    ph = ",".join("?" * len(unique))
    live = {
        r[0]
        for r in conn.execute(
            f"SELECT track_id FROM music_playlist_tracks "
            f"WHERE guild_id = ? AND platform = ? AND playlist_id = ? "
            f"AND removed_at IS NULL AND track_id IN ({ph})",
            (guild_id, platform, playlist_id, *unique),
        ).fetchall()
    }
    new: list[str] = []
    existing: list[str] = []
    seen: set[str] = set()
    for tid in track_ids:
        if tid in live or tid in seen:
            existing.append(tid)
        else:
            new.append(tid)
            seen.add(tid)
    return new, existing


def trim_to_window(
    conn: sqlite3.Connection,
    guild_id: int,
    playlist_id: str,
    window_size: int,
    *,
    platform: str = PLATFORM_SPOTIFY,
    removed_at: float | None = None,
) -> list[Mapping[str, Any]]:
    """Roll the oldest live rows off until *window_size* remain.

    Returns exactly the overflow, oldest first — the rows the caller must
    now remove from the actual playlist. They are already marked removed
    (``rolled_off``) on return, so a crash between DB and Spotify leaves the
    DB conservative: the window never over-reports what's live.
    """
    if window_size < 0:
        raise ValueError("window_size can't be negative")
    overflow_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM music_playlist_tracks "
            "WHERE guild_id = ? AND platform = ? AND playlist_id = ? "
            "AND removed_at IS NULL ORDER BY id DESC LIMIT -1 OFFSET ?",
            (guild_id, platform, playlist_id, window_size),
        ).fetchall()
    ]
    if not overflow_ids:
        return []
    ph = ",".join("?" * len(overflow_ids))
    rows = conn.execute(
        f"SELECT * FROM music_playlist_tracks WHERE id IN ({ph}) ORDER BY id",
        overflow_ids,
    ).fetchall()
    ts = time.time() if removed_at is None else removed_at
    conn.execute(
        f"UPDATE music_playlist_tracks SET removed_at = ?, removal_reason = ? "
        f"WHERE id IN ({ph})",
        (ts, REASON_ROLLED_OFF, *overflow_ids),
    )
    # The rolled-off rows' tenure is over, so any duplicate references to the
    # same tracks are now stale. Left alive they resurrect on a later delete.
    for row in rows:
        retire_duplicate_references(
            conn,
            guild_id,
            playlist_id=str(row["playlist_id"]),
            track_id=str(row["track_id"]),
            platform=str(row["platform"]),
            removed_at=ts,
        )
    return rows


def retire_duplicate_references(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    playlist_id: str,
    track_id: str,
    platform: str = PLATFORM_SPOTIFY,
    removed_at: float | None = None,
    exclude_message_id: int | None = None,
) -> int:
    """Stop outstanding ``duplicate`` rows for a track from counting.

    Call this wherever a track's tenure in the window ends. Returns how many
    references were retired. They keep their row (the history of who posted
    what is intact) but move to ``stale_reference``, which is outside both the
    survivor lookup and the History view.
    """
    params: list[Any] = [
        time.time() if removed_at is None else removed_at,
        REASON_STALE_REFERENCE,
        guild_id, platform, playlist_id, track_id, REASON_DUPLICATE,
    ]
    sql = (
        "UPDATE music_playlist_tracks SET removed_at = ?, removal_reason = ? "
        "WHERE guild_id = ? AND platform = ? AND playlist_id = ? "
        "AND track_id = ? AND removal_reason = ?"
    )
    if exclude_message_id is not None:
        sql += " AND message_id != ?"
        params.append(exclude_message_id)
    return conn.execute(sql, params).rowcount


def remove_tracks_for_message(
    conn: sqlite3.Connection,
    guild_id: int,
    message_id: int,
    *,
    reason: str = REASON_MESSAGE_DELETED,
    removed_at: float | None = None,
) -> list[Mapping[str, Any]]:
    """A message is gone; return only the tracks that must leave the playlist.

    The two-people-posted-the-same-song rule: a track this message added
    stays in the playlist when another live message still references it
    (a ``duplicate`` row whose own message hasn't been deleted). That
    surviving reference is promoted to the live row — cleared of its
    removal mark, taking over the window slot — so deleting your post never
    silently revokes someone else's. Only tracks with no surviving
    reference are returned, marked removed with *reason*.
    """
    ts = time.time() if removed_at is None else removed_at
    # This message's own duplicate references stop counting first — they must
    # not rescue anything once their message is gone, including on a later
    # deletion of the message that holds the live row. They retire as
    # ``stale_reference``, NOT as *reason*: a reference never occupied the
    # window, so filing it as "message deleted" would list the track in History
    # as removed while it is still sitting in the Live Window.
    conn.execute(
        "UPDATE music_playlist_tracks SET removal_reason = ? "
        "WHERE guild_id = ? AND message_id = ? AND removal_reason = ?",
        (REASON_STALE_REFERENCE, guild_id, message_id, REASON_DUPLICATE),
    )
    live_rows = conn.execute(
        "SELECT * FROM music_playlist_tracks "
        "WHERE guild_id = ? AND message_id = ? AND removed_at IS NULL",
        (guild_id, message_id),
    ).fetchall()
    removed: list[Mapping[str, Any]] = []
    for row in live_rows:
        conn.execute(
            "UPDATE music_playlist_tracks SET removed_at = ?, "
            "removal_reason = ? WHERE id = ?",
            (ts, reason, row["id"]),
        )
        survivor = conn.execute(
            "SELECT id FROM music_playlist_tracks "
            "WHERE guild_id = ? AND platform = ? AND playlist_id = ? "
            "AND track_id = ? AND removal_reason = ? AND message_id != ? "
            "ORDER BY id LIMIT 1",
            (guild_id, row["platform"], row["playlist_id"], row["track_id"],
             REASON_DUPLICATE, message_id),
        ).fetchone()
        if survivor is not None:
            # Oldest surviving reference becomes the live row; the track
            # never leaves the playlist, so it is not in the return.
            conn.execute(
                "UPDATE music_playlist_tracks "
                "SET removed_at = NULL, removal_reason = NULL WHERE id = ?",
                (survivor["id"],),
            )
        else:
            # Track really is leaving. Nothing should still reference it.
            retire_duplicate_references(
                conn,
                guild_id,
                playlist_id=str(row["playlist_id"]),
                track_id=str(row["track_id"]),
                platform=str(row["platform"]),
                removed_at=ts,
            )
            removed.append(row)
    return removed


# ── Unmatched review queue ────────────────────────────────────────────


def create_unmatched(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    channel_id: int,
    message_id: int,
    source_url: str,
    added_by: int,
    extracted_title: str | None = None,
    extracted_channel: str | None = None,
    candidate_track_id: str | None = None,
    candidate_name: str | None = None,
    candidate_artist: str | None = None,
    confidence: float | None = None,
    reason: str | None = None,
    created_at: float | None = None,
) -> int:
    """Queue a link the resolver couldn't confidently match. Returns its id."""
    cur = conn.execute(
        "INSERT INTO music_playlist_unmatched "
        "(guild_id, channel_id, message_id, source_url, extracted_title, "
        " extracted_channel, candidate_track_id, candidate_name, "
        " candidate_artist, confidence, reason, added_by, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, channel_id, message_id, source_url, extracted_title,
         extracted_channel, candidate_track_id, candidate_name,
         candidate_artist, confidence, reason, added_by, STATUS_PENDING,
         time.time() if created_at is None else created_at),
    )
    return int(cur.lastrowid or 0)


def list_pending(
    conn: sqlite3.Connection, guild_id: int
) -> list[Mapping[str, Any]]:
    """Pending review items, oldest first — the order a reviewer works."""
    return conn.execute(
        "SELECT * FROM music_playlist_unmatched "
        "WHERE guild_id = ? AND status = ? ORDER BY id",
        (guild_id, STATUS_PENDING),
    ).fetchall()


def set_unmatched_status(
    conn: sqlite3.Connection,
    guild_id: int,
    item_id: int,
    status: str,
    *,
    reviewed_by: int,
    reviewed_at: float | None = None,
) -> bool:
    """Resolve a pending item. False when it isn't this guild's, or isn't pending.

    Only ``pending → approved/rejected`` exists: the ``status = 'pending'``
    guard makes two reviewers racing on one item resolve it exactly once
    (the loser gets False, not a silent overwrite), and a resolved item can
    never be flipped. Raises ``ValueError`` on a status that isn't a review
    verdict.
    """
    if status not in REVIEW_STATUSES:
        raise ValueError(f"unknown review status: {status!r}")
    cur = conn.execute(
        "UPDATE music_playlist_unmatched "
        "SET status = ?, reviewed_by = ?, reviewed_at = ? "
        "WHERE id = ? AND guild_id = ? AND status = ?",
        (status, reviewed_by,
         time.time() if reviewed_at is None else reviewed_at,
         item_id, guild_id, STATUS_PENDING),
    )
    return cur.rowcount > 0


# ── Erasure ───────────────────────────────────────────────────────────


def purge_member_rows(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> int:
    """Erase a member's rows from both tables. Returns rows deleted.

    Called from ``privacy_service.purge_user_data`` — the columns are named
    ``added_by``, which that sweep's generic ``user_id`` statements can't
    reach. Deletes rather than anonymizes (per the spec's register decision:
    no ground to preserve "who posted a song"); the Spotify playlist itself
    is untouched, and a window row deleted here simply stops being listed —
    the next reconcile squares the difference. A review the member merely
    *performed* is a mod action, not their data: ``reviewed_by`` is nulled,
    the item survives.
    """
    deleted = 0
    for table in ("music_playlist_tracks", "music_playlist_unmatched"):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE guild_id = ? AND added_by = ?",
            (guild_id, user_id),
        )
        deleted += max(cur.rowcount, 0)
    conn.execute(
        "UPDATE music_playlist_unmatched SET reviewed_by = NULL "
        "WHERE guild_id = ? AND reviewed_by = ?",
        (guild_id, user_id),
    )
    return deleted
