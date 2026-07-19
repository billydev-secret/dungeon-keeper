"""One-off: warm the kind-activity ledger + seed variety-round quests.

Plan docs/plans/quest-variety-and-community-weeklies.md, stage 2. Two jobs:

1. **Backfill `econ_kind_activity`** (all guilds) from the xp_events sources
   that mirror trigger kinds — text→message_sent, reply→reply_sent,
   reaction_given, voice→voice_session — over the trailing 70 days, bucketed
   to guild-local days. Idempotent: totals are recomputed from source and
   REPLACEd each run. QOTD has no distinct xp source and stays cold.

2. **Seed + activate** the stage-1 kind quests for the main guild, calibrated
   against the 2026-07-18 feasibility sweep (revives ~1.3/day expected under
   the new lull model, whisper solves ~3/day, guess solves ~4/day, game
   sessions ~1/day, bumps near-zero — the quest IS the nudge). Sparse kinds
   land as weekly/monthly so a 2-slot daily board never wastes a slot on a
   quest that can't fire that day. Idempotent by title.

RUN THIS ONLY AFTER THE BOT HAS RESTARTED onto stage-1 code: it refuses to
run until migration 080's table exists, because seeding new-kind quests under
the old build would break dashboard kind rendering (TRIGGER_KINDS[kind]).

Usage:
    .venv/bin/python scripts/seed_quest_variety.py [--db PATH] [--guild ID]
                                                   [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bot_modules.core.db_utils import get_tz_offset_hours, open_db  # noqa: E402
from bot_modules.economy.logic import local_day_for  # noqa: E402
from bot_modules.services.economy_quests_service import (  # noqa: E402
    create_quest,
    set_quest_active,
)
from bot_modules.services.economy_service import save_econ_settings  # noqa: E402

MAIN_GUILD = 1469491362444480666
BACKFILL_DAYS = 70

# xp_events source → trigger kind (the ledger's mirrored warm-start set).
SOURCE_TO_KIND = {
    "text": "message_sent",
    "reply": "reply_sent",
    "reaction_given": "reaction_given",
    "voice": "voice_session",
}

# title, qtype, kind, reward, reward_xp, target_count, description
SEEDS: list[tuple[str, str, str, int, int, int, str]] = [
    (
        "Drop a Track", "daily", "music_request", 12, 5, 1,
        "Queue up a song with /play — one bump per day, playlist or single.",
    ),
    (
        "Say It Out Loud", "daily", "voice_message", 15, 8, 1,
        "Post a voice message in chat. The bot even transcribes it for you.",
    ),
    (
        "Ember Answerer", "weekly", "chat_revive", 35, 15, 1,
        "When the ember glows 🔥, answer it — talk in a revived channel "
        "within the half hour.",
    ),
    (
        "Bump the Beacon", "weekly", "bump", 40, 20, 1,
        "Bump the server on a listing site. Cooldowns are the rate limit — "
        "one hero per bump.",
    ),
    (
        "Open House", "weekly", "voice_room_host", 50, 25, 1,
        "Spin up a voice room and host a real hangout — pays when 2+ members "
        "join you.",
    ),
    (
        "Game Night Regular", "weekly", "session_join", 35, 15, 1,
        "Show up for a game night — play in any party game session this week.",
    ),
    (
        "Whisper Sleuth", "weekly", "whisper_guess", 40, 20, 1,
        "Someone whispered you anonymously? Figure out who. Correct guesses "
        "only.",
    ),
    (
        "Face Detective", "weekly", "guess_win", 40, 20, 1,
        "Win a Guess Who round — first correct guess takes it.",
    ),
    (
        "Pen Pal Devotee", "monthly", "pen_pal_complete", 100, 50, 1,
        "See a Pen Pals match through to the very end — the full session, "
        "no early exit.",
    ),
    (
        "Certified Quotable", "monthly", "quoted", 80, 40, 1,
        "Say something worth framing — pays when someone else turns your "
        "message into a quote card.",
    ),
    (
        "Cake Day on File", "event", "birthday_set", 25, 10, 1,
        "Tell the bot your birthday so the server can celebrate you. Once "
        "ever.",
    ),
    (
        "Onwards & Upwards", "event", "level_up", 15, 0, 1,
        "Level up! Pays every time a new level lands. (No XP on this one — "
        "XP paying XP would spiral.)",
    ),
    (
        "Hot Seat Hero", "event", "ama_answer", 5, 2, 1,
        "Take the AMA hot seat and actually answer — pays per question you "
        "reply to.",
    ),
]


def backfill(conn: sqlite3.Connection, now: float, dry: bool) -> int:
    cutoff = now - BACKFILL_DAYS * 86400
    guilds = [
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT guild_id FROM xp_events WHERE created_at > ?",
            (cutoff,),
        )
    ]
    total = 0
    for gid in guilds:
        offset = get_tz_offset_hours(conn, gid)
        rows = conn.execute(
            "SELECT user_id, source, created_at FROM xp_events "
            "WHERE guild_id = ? AND created_at > ? AND source IN "
            "('text','reply','reaction_given','voice')",
            (gid, cutoff),
        ).fetchall()
        agg: dict[tuple[int, str, str], int] = {}
        for r in rows:
            kind = SOURCE_TO_KIND[r["source"]]
            day = local_day_for(float(r["created_at"]), offset)
            key = (int(r["user_id"]), kind, day)
            agg[key] = agg.get(key, 0) + 1
        total += len(agg)
        if dry:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO econ_kind_activity "
            "(guild_id, user_id, kind, local_day, count) VALUES (?, ?, ?, ?, ?)",
            [(gid, uid, kind, day, n) for (uid, kind, day), n in agg.items()],
        )
    return total


def seed(conn: sqlite3.Connection, guild_id: int, dry: bool) -> list[str]:
    created: list[str] = []
    for title, qtype, kind, reward, xp, target, desc in SEEDS:
        exists = conn.execute(
            "SELECT 1 FROM econ_quests WHERE guild_id = ? AND title = ?",
            (guild_id, title),
        ).fetchone()
        if exists:
            continue
        created.append(f"{title} [{qtype}/{kind}] {reward}c/{xp}xp")
        if dry:
            continue
        qid = create_quest(
            conn,
            guild_id,
            title=title,
            description=desc,
            qtype=qtype,
            reward=reward,
            signoff=0,
            criteria="",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=None,
            created_by=None,
            trigger_kind=kind,
            target_count=target,
            reward_xp=xp,
        )
        set_quest_active(conn, guild_id, qid, True)
    return created


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parent.parent / "dungeonkeeper.db"),
    )
    ap.add_argument("--guild", type=int, default=MAIN_GUILD)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open_db(Path(args.db)) as conn:
        guard = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'econ_kind_activity'"
        ).fetchone()
        if guard is None:
            print(
                "REFUSING: econ_kind_activity missing — migration 080 has not "
                "run. Restart the bot onto stage-1 code first."
            )
            return 1
        n = backfill(conn, time.time(), args.dry_run)
        created = seed(conn, args.guild, args.dry_run)
        # Set bonuses default OFF globally; the main guild opts in here
        # (stage-5 add-on, values from the plan's locked Q&A).
        if not args.dry_run:
            save_econ_settings(
                conn, args.guild,
                {"quest_set_bonus_daily": 10, "quest_set_bonus_weekly": 25},
            )

    mode = "DRY RUN — would write" if args.dry_run else "wrote"
    print(f"backfill: {mode} {n} (guild,user,kind,day) rows")
    for line in created:
        print(f"seeded: {line}")
    if not created:
        print("seeded: nothing (all titles already present)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
