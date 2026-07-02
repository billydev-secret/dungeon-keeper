"""Drama analysis commands.

Provides a single mod-only slash command:
  /chilling_effect — identify members whose arrival in a channel (after a gap)
                     correlates with other active members going quiet.

Uses the local message archive populated by /interaction_scan and on_message.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from typing import TYPE_CHECKING



if TYPE_CHECKING:
    pass

log = logging.getLogger("dungeonkeeper.drama")

_MSG_PREVIEW = 90  # max chars shown per message in the report
_EVENTS_PER_PERSON = 2  # example entry events shown per ranked person
_VICTIMS_PER_EVENT = 3  # silenced users shown per event


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# One victim's last message before the entrant arrived.
# (victim_id, ts, content | None)
_VictimMsg = tuple[int, int, str | None]

# One entry event.
# (channel_id, ts, author_id, content | None, silenced: list[_VictimMsg])
_EntryEvent = tuple[int, int, int, str | None, list[_VictimMsg]]


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def _analyze_chilling_effect(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    cutoff_ts: int,
    entry_gap_seconds: int,
    window_seconds: int,
    channel_id: int | None = None,
) -> tuple[list[_EntryEvent], int]:
    """
    Scan message history for 'entry events' — a user posts in a channel after
    being absent for at least entry_gap_seconds — and record which currently-
    active users stop posting in the following window.

    For each entry event, captures:
      - the entry message content
      - for each silenced user: their last message before the entry

    Returns (events, channel_count).
    """
    query = (
        "SELECT channel_id, author_id, ts, message_id, content "
        "FROM messages "
        "WHERE guild_id = ? AND ts >= ?"
    )
    params: list[object] = [guild_id, cutoff_ts]
    if channel_id is not None:
        query += " AND channel_id = ?"
        params.append(channel_id)
    query += " ORDER BY channel_id, ts ASC"

    rows = conn.execute(query, params).fetchall()

    # Group (ts, author_id, message_id, content) by channel
    # Using a list of tuples: (ts, author_id, message_id, content)
    msgs_by_channel: dict[int, list[tuple[int, int, int, str | None]]] = defaultdict(
        list
    )
    for row in rows:
        msgs_by_channel[int(row[0])].append(
            (int(row[2]), int(row[1]), int(row[3]), row[4])
        )

    events: list[_EntryEvent] = []

    for ch_id, ch_msgs in msgs_by_channel.items():
        ch_msgs.sort()

        last_ts_by_user: dict[int, int] = {}

        for i, (ts, author_id, _mid, content) in enumerate(ch_msgs):
            prev_ts = last_ts_by_user.get(author_id)
            last_ts_by_user[author_id] = ts

            if prev_ts is None or (ts - prev_ts) < entry_gap_seconds:
                continue

            # Scan backward for [ts - window, ts): capture each other user's
            # most recent message (the one they'll be "silenced" from).
            last_before: dict[
                int, tuple[int, str | None]
            ] = {}  # victim -> (ts, content)
            for j in range(i - 1, -1, -1):
                jts, jauthor, _jmid, jcontent = ch_msgs[j]
                if jts < ts - window_seconds:
                    break
                if jauthor != author_id and jauthor not in last_before:
                    last_before[jauthor] = (jts, jcontent)

            if not last_before:
                continue

            # Scan forward for [ts, ts + window): find who does post.
            active_after: set[int] = set()
            for j in range(i + 1, len(ch_msgs)):
                jts, jauthor, _jmid, _jcontent = ch_msgs[j]
                if jts >= ts + window_seconds:
                    break
                if jauthor != author_id:
                    active_after.add(jauthor)

            silenced_victims: list[_VictimMsg] = [
                (victim, victim_ts, victim_content)
                for victim, (victim_ts, victim_content) in last_before.items()
                if victim not in active_after
            ]

            if not silenced_victims:
                continue

            # Sort victims by most recent last-message first
            silenced_victims.sort(key=lambda v: v[1], reverse=True)
            events.append((ch_id, ts, author_id, content, silenced_victims))

    return events, len(msgs_by_channel)


def _fmt(ts: int) -> str:
    """Discord timestamp — renders as HH:MM in the viewer's local time."""
    return f"<t:{ts}:t>"


def _preview(text: str | None, limit: int = _MSG_PREVIEW) -> str:
    if not text:
        return "_[no text]_"
    text = text.replace("\n", " ")
    return text[:limit] + "…" if len(text) > limit else text
