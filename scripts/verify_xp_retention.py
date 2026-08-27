#!/usr/bin/env python3
"""Prove, on a copy of the real database, that pruning xp_events changes nothing.

Stage 3 of ``docs/plans/xp-events-retention-and-rollup.md`` deletes raw
``xp_events`` rows that a daily rollup has already summarised. The tests pin the
behaviour on synthetic data; this pins it on **the actual history**, which is
the only place a real gap in the rollup, an odd NULL channel or a float that
sums differently in two orders will show up.

It works on a **copy**, never the live file, and it refuses to open
``dungeonkeeper.db``. Take the copy with the sqlite3 **backup API** — a plain
``cp`` of a live WAL database reads as malformed, and the snapshot wants ~1GB,
so put it on ``/home`` and not in a tmpfs scratch dir::

    python - <<'PY'
    import sqlite3
    src = sqlite3.connect("file:dungeonkeeper.db?mode=ro", uri=True)
    dst = sqlite3.connect("/home/ben/xp-snap.db")
    src.backup(dst); dst.close(); src.close()
    PY
    python -c "import sys; sys.path.insert(0,'src'); \
      from migrations import apply_migrations_sync; \
      apply_migrations_sync('/home/ben/xp-snap.db')"
    python scripts/verify_xp_retention.py --db /home/ben/xp-snap.db

(The migration step is only needed while ``xp_daily`` has not reached prod yet.)

What it does, in order:

1. Rolls up every complete day (the backfill Stage 1's loop would do).
2. Snapshots every reader that unions the rollup, for every guild.
3. Deletes the raw rows below the retention boundary — the real
   ``prune_raw_events``, guards and all, not a hand-written DELETE.
4. Snapshots again and diffs.

A clean run means shipping retention does not move a single number a member or
a moderator can see. A dirty run names the reader, the guild and the key that
disagreed, which is the only useful form of that answer.

Exit status is 0 when the snapshots match and 1 when they do not, so this can
gate the dial being turned on.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.xp_system import (  # noqa: E402
    get_time_to_level_details,
    get_user_xp_by_source,
    get_user_xp_standing,
    get_xp_distribution_stats,
    get_xp_leaderboard,
    has_any_xp_events,
)
from bot_modules.services import xp_rollup_service as rollup  # noqa: E402
from bot_modules.services.activity_graphs import (  # noqa: E402
    query_xp_activity_with_breakdown,
)
from bot_modules.services.inactive_report_service import (  # noqa: E402
    channel_activity_map,
)

# Enough of the long tail to be meaningful without reading the whole guild.
TOP_USERS = 25
TOP_CHANNELS = 5


def _guilds(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT guild_id, COUNT(*) c FROM xp_events GROUP BY guild_id ORDER BY c DESC"
        )
    ]


def _sources(conn: sqlite3.Connection, guild_id: int) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT source FROM xp_events WHERE guild_id = ? ORDER BY source",
            (guild_id,),
        )
    ]


def _top_users(conn: sqlite3.Connection, guild_id: int) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT user_id, SUM(amount) s FROM xp_events WHERE guild_id = ?"
            " GROUP BY user_id ORDER BY s DESC LIMIT ?",
            (guild_id, TOP_USERS),
        )
    ]


def _top_channels(conn: sqlite3.Connection, guild_id: int) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT channel_id, COUNT(*) c FROM xp_events"
            " WHERE guild_id = ? AND channel_id IS NOT NULL"
            " GROUP BY channel_id ORDER BY c DESC LIMIT ?",
            (guild_id, TOP_CHANNELS),
        )
    ]


def _snapshot(conn: sqlite3.Connection, plan: dict) -> dict:
    """Every unioning reader's output, keyed so a diff names what broke.

    The shape is fixed up front by ``plan`` — the guilds, sources, users and
    channels to ask about are chosen *before* the prune, so the second pass
    asks exactly the same questions even though the table it reads has changed
    underneath it.
    """
    out: dict[str, object] = {}
    for guild_id, spec in plan.items():
        g = f"g{guild_id}"
        out[f"{g}/has_events"] = has_any_xp_events(conn, guild_id)
        out[f"{g}/time_to_level_5"] = [
            (r["user_id"], round(r["seconds"] / 86400.0, 3))
            for r in get_time_to_level_details(conn, guild_id, 5)
        ]
        # Bucketed graphs at the 360-day reach — the resolution the rollup's
        # day-granularity skew can actually touch.
        labels, totals, members, by_source = query_xp_activity_with_breakdown(
            conn, guild_id, "month"
        )
        out[f"{g}/month_total"] = round(sum(totals), 2)
        out[f"{g}/month_by_source_total"] = {
            k: round(sum(v), 2) for k, v in sorted(by_source.items())
        }
        out[f"{g}/month_members_total"] = sum(members)

        for source in spec["sources"]:
            out[f"{g}/leaderboard/{source}"] = [
                (e.user_id, e.xp)
                for e in get_xp_leaderboard(conn, guild_id, source, limit=TOP_USERS)
            ]
            out[f"{g}/distribution/{source}"] = get_xp_distribution_stats(
                conn, guild_id, source
            )
        for user_id in spec["users"]:
            out[f"{g}/by_source/{user_id}"] = get_user_xp_by_source(
                conn, guild_id, user_id
            )
            for source in spec["sources"]:
                out[f"{g}/standing/{source}/{user_id}"] = get_user_xp_standing(
                    conn, guild_id, source, user_id
                )
        for channel_id in spec["channels"]:
            out[f"{g}/last_active/{channel_id}"] = {
                uid: a.created_at
                for uid, a in channel_activity_map(
                    conn, guild_id, spec["users"], channel_id
                ).items()
            }
    return out


def _diff(before: dict, after: dict) -> list[str]:
    problems = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key, "<missing>"), after.get(key, "<missing>")
        if b != a:
            problems.append(f"  {key}\n    before: {b!r}\n    after:  {a!r}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to a SNAPSHOT, not the live DB")
    ap.add_argument(
        "--keep-raw",
        action="store_true",
        help="roll up and compare, but skip the deletion step",
    )
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 2
    if path.resolve() == (PROJECT_ROOT / "dungeonkeeper.db").resolve():
        print(
            "refusing to run against the live database — take a backup-API "
            "snapshot first (recipe in this script's docstring)",
            file=sys.stderr,
        )
        return 2

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    guilds = _guilds(conn)
    print(f"{len(guilds)} guild(s) with XP events")
    raw_before = conn.execute("SELECT COUNT(*) FROM xp_events").fetchone()[0]

    t0 = time.time()
    days, buckets = rollup.rollup_pending_days(conn)
    rollup.recompute_watermark(conn)
    conn.commit()
    print(
        f"rolled up {days} day(s) → {buckets} bucket(s) in {time.time() - t0:.1f}s"
    )

    stats = rollup.rollup_stats(conn)
    if stats["days_missing"]:
        print(f"!! {len(stats['days_missing'])} day(s) with events and no rollup")
        print(f"   first: {stats['days_missing'][:5]}")
    print(
        f"rollup covers {stats['events_covered']} of {stats['raw_events']} events, "
        f"{stats['xp_covered']:.2f} XP"
    )

    plan = {
        gid: {
            "sources": _sources(conn, gid),
            "users": _top_users(conn, gid),
            "channels": _top_channels(conn, gid),
        }
        for gid in guilds
    }

    print("snapshotting readers before the prune…")
    before = _snapshot(conn, plan)

    if args.keep_raw:
        print("--keep-raw: stopping before the deletion step")
        return 0

    deleted = 0
    for gid in guilds:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value, guild_id) VALUES (?, '1', ?)",
            (rollup.RETENTION_CONFIG_KEY, gid),
        )
        while True:
            try:
                n = rollup.prune_raw_events(conn, gid, limit=100_000)
            except rollup.PruneRefused as exc:
                print(f"!! guild {gid}: prune refused — {exc}")
                break
            if not n:
                break
            deleted += n
    conn.commit()

    raw_after = conn.execute("SELECT COUNT(*) FROM xp_events").fetchone()[0]
    print(f"pruned {deleted} row(s): xp_events {raw_before} → {raw_after}")

    print("snapshotting readers after the prune…")
    after = _snapshot(conn, plan)

    problems = _diff(before, after)
    if problems:
        print(f"\nFAIL — {len(problems)} reader(s) disagree:\n")
        print("\n".join(problems[:40]))
        if len(problems) > 40:
            print(f"  … and {len(problems) - 40} more")
        return 1

    print(f"\nOK — all {len(before)} reader outputs identical across the prune.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
