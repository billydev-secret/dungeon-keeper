"""Guess — the inactivity nudge behind ``guess_inactivity_ping_hours``.

The dial ("Hours of silence before a nudge") has been offered by the Config
Advisor since the Guess feature shipped, and until now nothing read it. This
module is the reader.

The nudge is deliberately narrow: it fires for an **open round that has gone
quiet**, never for an empty channel. A ping that says "come play" when there is
nothing posted to guess is noise, and the dial's own wording is about silence
on something already running. So the decision is:

* the guild has a Guess channel, a Guess role to ping, and a positive hour
  count — any of those missing and the nudge is off;
* pick the oldest round that is unsolved, not deleted, not answer-opted-out,
  and whose last activity (the round going up, or its newest guess) is at
  least that many hours ago;
* ping once per round. A round nobody solves must not re-ping every tick, so
  the last-nudged round id is remembered in config and skipped.

Everything here is sync sqlite3 against an open connection, mirroring
``guess_repo`` — the cog does the ``asyncio.to_thread`` and the posting.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from bot_modules.core.db_utils import get_config_value, set_config_value

#: Config key holding the id of the round this guild was last nudged about.
#: Internal state, not an admin dial — no panel surfaces it.
NUDGED_ROUND_KEY = "guess_last_nudged_round_id"

#: The dial itself, as the Config Advisor spells it.
INACTIVITY_HOURS_KEY = "guess_inactivity_ping_hours"


@dataclass(frozen=True)
class StalledRound:
    """An open Guess round that has gone quiet long enough to nudge about."""

    round_id: int
    guild_id: int
    channel_id: int
    message_id: int
    role_id: int
    #: Unix seconds of the newest activity — the round, or its latest guess.
    last_activity_at: float

    @property
    def quiet_hours(self) -> float:
        return max(0.0, (time.time() - self.last_activity_at) / 3600.0)


def _int_config(conn: sqlite3.Connection, key: str, guild_id: int) -> int:
    try:
        return int(get_config_value(conn, key, "0", guild_id) or 0)
    except (TypeError, ValueError):
        return 0


def find_stalled_round(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> StalledRound | None:
    """The oldest open round this guild should be nudged about, if any.

    Returns ``None`` whenever the nudge is switched off, nothing qualifies, or
    the only candidate is the round we already nudged about.
    """
    hours = _int_config(conn, INACTIVITY_HOURS_KEY, guild_id)
    if hours <= 0:
        return None

    channel_id = _int_config(conn, "guess_channel_id", guild_id)
    role_id = _int_config(conn, "guess_role_id", guild_id)
    if channel_id == 0 or role_id == 0:
        return None

    cutoff = (time.time() if now is None else now) - hours * 3600.0
    already = _int_config(conn, NUDGED_ROUND_KEY, guild_id)

    # MAX(created_at, newest guess) is the round's last activity. LEFT JOIN so a
    # round nobody has guessed on still counts, using its own age.
    row = conn.execute(
        """
        SELECT r.id            AS round_id,
               r.channel_id    AS channel_id,
               r.message_id    AS message_id,
               MAX(r.created_at, COALESCE(MAX(g.created_at), r.created_at))
                               AS last_activity_at
        FROM guess_rounds r
        LEFT JOIN guess_guesses g ON g.round_id = r.id
        WHERE r.guild_id = ?
          AND r.deleted_at IS NULL
          AND r.solved_at IS NULL
          AND r.answer_optout = 0
          AND r.id != ?
        GROUP BY r.id
        HAVING last_activity_at <= ?
        ORDER BY last_activity_at ASC
        LIMIT 1
        """,
        (guild_id, already, cutoff),
    ).fetchone()
    if row is None:
        return None

    return StalledRound(
        round_id=row["round_id"],
        guild_id=guild_id,
        # A round posted before the channel was repointed still lives where it
        # was posted, so its own channel_id wins over the configured one.
        channel_id=row["channel_id"] or channel_id,
        message_id=row["message_id"] or 0,
        role_id=role_id,
        last_activity_at=row["last_activity_at"],
    )


def record_nudge(conn: sqlite3.Connection, guild_id: int, round_id: int) -> None:
    """Remember the round we just nudged about so it is only pinged once."""
    set_config_value(conn, NUDGED_ROUND_KEY, str(round_id), guild_id)


def build_nudge_content(stalled: StalledRound, *, guild_id: int) -> str:
    """The nudge message: a role ping and, where possible, a jump link.

    Kept to plain content rather than an embed — it is a one-line bump, and a
    jump link in an embed description doesn't preview.
    """
    hours = int(stalled.quiet_hours)
    when = "a while" if hours < 1 else (
        "an hour" if hours == 1 else f"{hours} hours"
    )
    parts = [
        f"<@&{stalled.role_id}> the Guess round has been quiet for {when} —"
        " nobody has cracked it yet."
    ]
    if stalled.message_id:
        parts.append(
            "https://discord.com/channels/"
            f"{guild_id}/{stalled.channel_id}/{stalled.message_id}"
        )
    return " ".join(parts)
