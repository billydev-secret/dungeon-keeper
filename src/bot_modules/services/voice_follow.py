"""Directed voice-follow capture.

Records when a member joins a voice channel another member is *already* in — a
directed "came to where they were" signal (``from_user_id`` → ``to_user_id``).
Join order supplies the direction that the symmetric ``voice_partner`` quest
trigger lacks, which is what makes this usable for the one-sided /
unreciprocated-attention report.

The recording deliberately drops weak or noisy joins:
  - Joining an empty channel records nothing (nobody was there to follow).
  - Joining a channel that already holds more than ``MAX_PRESENT`` people is a
    party, not pursuit, so it is ignored.
  - Rapid leave/rejoin flapping into the same channel (or duplicate gateway
    events) is debounced within ``DEBOUNCE_SECONDS`` so a single restless
    session cannot inflate the weight.

Schema lives in migration ``117_voice_follow.sql``.
"""

from __future__ import annotations

import sqlite3
import time as _time

# A join into a busy channel says little about any one person there, so skip
# the follow entirely once this many humans are already present.
MAX_PRESENT = 6

# Do not re-count the same ordered pair into the same channel within this
# window. Guards against leave/rejoin flapping and duplicate voice-state events.
DEBOUNCE_SECONDS = 600


def record_voice_follow(
    conn: sqlite3.Connection,
    guild_id: int,
    from_user_id: int,
    present_user_ids: list[int],
    channel_id: int,
    ts: int | None = None,
    *,
    max_present: int = MAX_PRESENT,
    debounce_seconds: int = DEBOUNCE_SECONDS,
) -> int:
    """Record *from_user_id* joining a channel already holding *present_user_ids*.

    ``present_user_ids`` is the set of humans (bots and the joiner excluded by
    the caller) who were in the channel when the joiner arrived.

    Returns the number of directed follow events actually written — 0 when the
    channel was empty, the crowd was too large, or every candidate pair was
    debounced.
    """
    targets = {uid for uid in present_user_ids if uid != from_user_id}
    if not targets:
        return 0
    if len(targets) > max_present:
        return 0  # crowd, not pursuit
    ts = ts if ts is not None else int(_time.time())

    recorded = 0
    for to_user_id in targets:
        # Debounce: skip if this ordered pair already landed in this channel
        # within the window.
        already = conn.execute(
            """
            SELECT 1 FROM voice_follow_log
            WHERE guild_id = ? AND from_user_id = ? AND to_user_id = ?
              AND channel_id = ? AND ts > ?
            LIMIT 1
            """,
            (guild_id, from_user_id, to_user_id, channel_id, ts - debounce_seconds),
        ).fetchone()
        if already is not None:
            continue

        conn.execute(
            """
            INSERT INTO voice_follow_log
                (guild_id, from_user_id, to_user_id, channel_id, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, from_user_id, to_user_id, channel_id, ts),
        )
        conn.execute(
            """
            INSERT INTO voice_follow
                (guild_id, from_user_id, to_user_id, weight, last_ts)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(guild_id, from_user_id, to_user_id)
            DO UPDATE SET weight = weight + 1, last_ts = excluded.last_ts
            """,
            (guild_id, from_user_id, to_user_id, ts),
        )
        recorded += 1

    return recorded
