#!/usr/bin/env python3
"""Seed the Pen Pals pool from members whose last chat they actually used.

Companion to the expiry requeue (migration 160). That fix stops the pool
draining, but it only fires when a session *expires* — and The Golden Meadow
has no active sessions left to expire, so on its own the fix leaves the pool
sitting at one member forever. A drained pool cannot refill itself. This is the
one-off seed that restarts it.

Who gets pooled: every member whose **most recent** pen pal session is closed
and who posted at least one message in it — the same engagement test
``_member_spoke_in_session`` applies at expiry, so the seed and the ongoing
behaviour agree about who wants to be here. Members already pooled or in an
active session are skipped, which also makes the script idempotent: running it
twice pools nobody the second time.

``joined_at`` is backdated to each member's last session close rather than set
to now, so the pool's FIFO order reflects who has actually been waiting
longest. The re-match cooldown is *not* bypassed — a member whose chat ended
inside the cooldown window sits in the pool, ineligible, exactly as they would
after a normal expiry.

No DMs. This writes pool rows and their audit events and nothing else; members
find out the same way they would from any other match.

    python scripts/backfill_pen_pals_pool.py --guild 1469491362444480666
    python scripts/backfill_pen_pals_pool.py --guild 1469... --apply

Default is a dry run that prints exactly what it would do.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

#: The reason stamped on every pool event this script writes, so the Pool
#: Activity list distinguishes a seeded member from one who signed up.
REASON = "backfill"


def candidates(conn: sqlite3.Connection, guild_id: int) -> list[tuple[int, float]]:
    """(user_id, last_closed_at) for everyone the seed should pool, oldest first.

    Pure read. Kept separate from the write so the selection rule is testable
    without a database that anyone minds being written to.
    """
    rows = conn.execute(
        """
        SELECT channel_id, user1_id, user2_id, closed_at, state
        FROM pen_pals_sessions WHERE guild_id = ?
        ORDER BY COALESCE(closed_at, started_at)
        """,
        (guild_id,),
    ).fetchall()

    last: dict[int, tuple[int, float]] = {}  # user -> (channel of last session, closed_at)
    busy: set[int] = set()
    for channel_id, u1, u2, closed_at, state in rows:
        for uid in (u1, u2):
            if state == "active":
                busy.add(uid)
            elif closed_at is not None:
                last[uid] = (channel_id, closed_at)

    pooled = {
        r[0] for r in conn.execute(
            "SELECT user_id FROM pen_pals_pool WHERE guild_id = ?", (guild_id,)
        )
    }

    out: list[tuple[int, float]] = []
    for uid, (channel_id, closed_at) in last.items():
        if uid in busy or uid in pooled:
            continue
        spoke = conn.execute(
            "SELECT 1 FROM messages WHERE guild_id = ? AND channel_id = ? "
            "AND author_id = ? LIMIT 1",
            (guild_id, channel_id, uid),
        ).fetchone()
        if spoke:
            out.append((uid, float(closed_at)))
    return sorted(out, key=lambda t: t[1])


def apply(conn: sqlite3.Connection, guild_id: int, picks: list[tuple[int, float]]) -> None:
    for uid, joined_at in picks:
        conn.execute(
            "INSERT OR IGNORE INTO pen_pals_pool (guild_id, user_id, joined_at) "
            "VALUES (?, ?, ?)",
            (guild_id, uid, joined_at),
        )
        conn.execute(
            "INSERT INTO pen_pals_pool_events (guild_id, user_id, at, action, reason) "
            "VALUES (?, ?, ?, 'join', ?)",
            (guild_id, uid, joined_at, REASON),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--guild", type=int, required=True)
    ap.add_argument("--db", type=Path, default=REPO / "dungeonkeeper.db")
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args()

    uri = f"file:{args.db}?mode={'rw' if args.apply else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        picks = candidates(conn, args.guild)
        pooled_now = conn.execute(
            "SELECT count(*) FROM pen_pals_pool WHERE guild_id = ?", (args.guild,)
        ).fetchone()[0]

        print(f"guild {args.guild}: {pooled_now} already pooled, {len(picks)} to seed")
        for uid, at in picks:
            print(f"  + {uid}  (last chat ended {at:.0f})")
        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply.")
            return 0
        apply(conn, args.guild, picks)
        conn.commit()
        print(f"\nSeeded {len(picks)}. Pool is now {pooled_now + len(picks)}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
