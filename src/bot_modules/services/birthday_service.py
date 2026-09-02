"""Birthday tracker — DB helpers extracted from birthday_cog for testability."""

from __future__ import annotations

import calendar
import re
import sqlite3
import time
from dataclasses import dataclass

from bot_modules.core.db_utils import get_config_value

# Max valid day per month; Feb capped at 28 (Feb 29 skips 3/4 years)
MAX_DAYS = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def month_choices() -> list[tuple[str, int]]:
    """(name, number) for every month, in calendar order.

    The set-birthday modal renders these as a select. Twelve options sit
    well inside Discord's 25-option cap, which is what lets the month stop
    being a typed number — and takes the "must be between 1 and 12" error
    with it.
    """
    return [(calendar.month_name[m], m) for m in range(1, 13)]


def parse_birthday_day(raw: str, month: int) -> tuple[int | None, str | None]:
    """A day-of-month from the modal's text box: ``(day, error)``.

    The month arrives from a select and is trusted. The day is still typed
    because 31 values overflow the select cap, and because its upper bound
    depends on which month was picked.
    """
    try:
        day = int(raw.strip())
    except ValueError:
        return None, "❌ Day must be a whole number."
    if not 1 <= day <= MAX_DAYS[month]:
        return None, (
            f"❌ {calendar.month_name[month]} has at most "
            f"{MAX_DAYS[month]} days."
        )
    return day, None

# Guild-local hour the announcement pass waits for. Configurable per guild on
# the Birthdays panel; 09:00 stays the default so guilds that never touch the
# dial keep the behavior they've always had.
ANNOUNCE_HOUR_KEY = "birthday_announce_hour"
DEFAULT_ANNOUNCE_HOUR = 9


def announce_hour(conn: sqlite3.Connection, guild_id: int) -> int:
    """Guild-local hour (0–23) at which today's birthdays go out.

    A missing, non-numeric or out-of-range stored value falls back to
    :data:`DEFAULT_ANNOUNCE_HOUR` — the loop must never stall on a bad row.
    """
    raw = get_config_value(conn, ANNOUNCE_HOUR_KEY, str(DEFAULT_ANNOUNCE_HOUR), guild_id)
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_ANNOUNCE_HOUR
    if not 0 <= hour <= 23:
        return DEFAULT_ANNOUNCE_HOUR
    return hour


# ── Announcement channels (migration 200) ────────────────────────────
#
# Was a fixed main + second channel (birthday_channel_id[_2] /
# birthday_message[_2] / birthday_pin[_2] config keys); now any number of
# rows here, one per announced channel, in the order they were added — same
# shape needle_channels uses for the same "add any number of channels" idiom.


@dataclass
class BirthdayChannelConfig:
    channel_id: int
    message: str
    pin: bool


def list_channels(
    conn: sqlite3.Connection, guild_id: int
) -> list[BirthdayChannelConfig]:
    """Every channel this guild announces birthdays to, oldest-added first."""
    rows = conn.execute(
        "SELECT channel_id, message, pin FROM birthday_channels "
        "WHERE guild_id = ? ORDER BY id",
        (guild_id,),
    ).fetchall()
    return [
        BirthdayChannelConfig(
            channel_id=row["channel_id"], message=row["message"], pin=bool(row["pin"])
        )
        for row in rows
    ]


def upsert_channel(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    channel_id: int,
    message: str,
    pin: bool,
) -> None:
    """Add a channel, or update it in place if it's already configured."""
    conn.execute(
        """
        INSERT INTO birthday_channels (guild_id, channel_id, message, pin)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (guild_id, channel_id) DO UPDATE SET
            message = excluded.message,
            pin     = excluded.pin
        """,
        (guild_id, channel_id, message, int(pin)),
    )


def delete_channel(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> bool:
    """Stop announcing in this channel. True if a row was actually removed."""
    cur = conn.execute(
        "DELETE FROM birthday_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    return (cur.rowcount or 0) > 0


# Matched anywhere in a message for the `birthday_wish` quest detector — the
# is_greeting pattern: a heuristic vocabulary, not a classifier; widen as real
# misses surface. Kept deliberately narrow ("happy birthday", "hbd", "happy
# bday/b-day/cake day") so ordinary chat on a birthday can't fire it.
_BIRTHDAY_WISH_RE = re.compile(
    r"\b(?:"
    r"hap+y+\s*(?:birthday+|bday+|b-day+|cake\s*day+)"
    r"|hbd"
    r"|feliz\s*cumplea[ñn]os"
    r"|joyeux\s*anniversaire"
    r")\b",
    re.IGNORECASE,
)


def is_birthday_wish(content: str) -> bool:
    """True if *content* reads as a happy-birthday wish."""
    return bool(content) and _BIRTHDAY_WISH_RE.search(content) is not None


def announced_birthday_ids(
    conn: sqlite3.Connection, guild_id: int, local_day: str
) -> set[int]:
    """Members whose birthday was publicly announced on this guild-local day.

    The `birthday_wish` quest gates on this rather than raw `member_birthdays`
    rows so a member with a quiet/unset birthday never becomes quest bait —
    only birthdays the bot itself put in front of the server count.
    """
    rows = conn.execute(
        "SELECT user_id FROM birthday_announcements "
        "WHERE guild_id = ? AND announced_date = ?",
        (guild_id, local_day),
    ).fetchall()
    return {int(r[0]) for r in rows}


def upsert_birthday(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    month: int,
    day: int,
    set_by: int,
    preference: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO member_birthdays (guild_id, user_id, birth_month, birth_day, set_by, set_at, preference)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            birth_month = excluded.birth_month,
            birth_day   = excluded.birth_day,
            set_by      = excluded.set_by,
            set_at      = excluded.set_at,
            preference  = excluded.preference
        """,
        (guild_id, user_id, month, day, set_by, time.time(), preference),
    )


def delete_birthday(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> bool:
    cur = conn.execute(
        "DELETE FROM member_birthdays WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return (cur.rowcount or 0) > 0


def list_all_birthdays(
    conn: sqlite3.Connection, guild_id: int
) -> list[tuple[int, int, int, str | None]]:
    rows = conn.execute(
        "SELECT user_id, birth_month, birth_day, preference FROM member_birthdays "
        "WHERE guild_id = ? ORDER BY birth_month, birth_day",
        (guild_id,),
    ).fetchall()
    return [
        (row["user_id"], row["birth_month"], row["birth_day"], row["preference"])
        for row in rows
    ]


def has_birthday(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    """Whether a birthday is on file at all.

    Distinct from ``get_birthday_preference``, which returns ``None`` both for
    "no birthday stored" and for "stored, with no preference set" — a
    difference that matters to ``/info``, where the two states offer opposite
    buttons.
    """
    return (
        conn.execute(
            "SELECT 1 FROM member_birthdays WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        is not None
    )


def get_birthday_preference(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> str | None:
    row = conn.execute(
        "SELECT preference FROM member_birthdays WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return row["preference"] if row else None


def todays_unannounced(
    conn: sqlite3.Connection,
    guild_id: int,
    month: int,
    day: int,
    date_iso: str,
) -> list[int]:
    """Return user_ids whose birthday is today and haven't been announced yet."""
    rows = conn.execute(
        """
        SELECT b.user_id
        FROM member_birthdays b
        LEFT JOIN birthday_announcements a
            ON a.guild_id = b.guild_id AND a.user_id = b.user_id AND a.announced_date = ?
        WHERE b.guild_id = ? AND b.birth_month = ? AND b.birth_day = ? AND a.user_id IS NULL
        """,
        (date_iso, guild_id, month, day),
    ).fetchall()
    return [row["user_id"] for row in rows]


def mark_announced(
    conn: sqlite3.Connection, guild_id: int, user_id: int, date_iso: str
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO birthday_announcements (guild_id, user_id, announced_date) VALUES (?, ?, ?)",
        (guild_id, user_id, date_iso),
    )
    return (cur.rowcount or 0) > 0


# ── Pin tracking ───────────────────────────────────────────────────────


def record_pin(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    date_iso: str,
) -> None:
    """Remember a pinned birthday message so a later pass can unpin it."""
    conn.execute(
        "INSERT OR REPLACE INTO birthday_pins "
        "(guild_id, channel_id, message_id, pinned_date) VALUES (?, ?, ?, ?)",
        (guild_id, channel_id, message_id, date_iso),
    )


def pins_before(
    conn: sqlite3.Connection, guild_id: int, before_iso: str
) -> list[tuple[int, int]]:
    """Return (channel_id, message_id) of pins recorded before ``before_iso``."""
    rows = conn.execute(
        "SELECT channel_id, message_id FROM birthday_pins "
        "WHERE guild_id = ? AND pinned_date < ?",
        (guild_id, before_iso),
    ).fetchall()
    return [(row["channel_id"], row["message_id"]) for row in rows]


def clear_pin(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, message_id: int
) -> None:
    """Drop a tracked pin row once the message has been unpinned (or is gone)."""
    conn.execute(
        "DELETE FROM birthday_pins "
        "WHERE guild_id = ? AND channel_id = ? AND message_id = ?",
        (guild_id, channel_id, message_id),
    )
