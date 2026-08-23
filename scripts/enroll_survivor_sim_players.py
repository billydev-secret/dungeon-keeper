"""One-off carry-over of the sim-season roster into the real 2026 season.

The sim run (season 2, "sim season") proved the engine with ten real members;
Billy's call (2026-08-22): refund the sim gauntlet fees (done —
``refund_survivor_test_seasons.py``) and keep everyone enrolled for the real
season (season 3, year 2026, free entry per the season-one decision). This
enrolls every season-2 player into the target season as ``alive`` and fixes
their Discord roles: everyone gets the Survivor role, and anyone still wearing
the sim's Ghost role loses it (a real season starts everyone alive).

Usage (dry run first — it writes nothing without --apply):

    python -m scripts.enroll_survivor_sim_players --from-season 2 --to-season 3
    python -m scripts.enroll_survivor_sim_players --from-season 2 --to-season 3 --apply

* **Idempotent** — enrollment rides ``add_player``'s INSERT OR IGNORE, and
  role changes are skipped when already correct.
* **No join echo, no buy-in.** The Event Echo is for organic joins; a bulk
  carry-over echoing ten times would be noise. Buy-in is whatever the target
  season's config says — this script refuses to run if it isn't 0, because a
  silent bulk debit is never okay.
* Roles go through the plain REST API (the bot process owns the gateway; this
  is a sibling process, same pattern as the other one-off scripts).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import open_db_immediate  # noqa: E402
from bot_modules.services import survivor_service as svc  # noqa: E402
from bot_modules.services.message_store import (  # noqa: E402
    get_known_user_names_bulk,
)

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"
API = "https://discord.com/api/v10"


def _token() -> str:
    for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"DISCORD_TOKEN_PROD\s*=\s*(.+)", line)
        if m:
            return m.group(1).strip().strip("\"'").split("#")[0].strip()
    sys.exit("DISCORD_TOKEN_PROD not found in .env")


def _member_roles(tok: str, guild_id: int, user_id: int) -> set[int] | None:
    req = urllib.request.Request(
        f"{API}/guilds/{guild_id}/members/{user_id}",
        headers={"Authorization": f"Bot {tok}",
                 "User-Agent": "DiscordBot (dk-scripts, 1.0)"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return {int(r) for r in json.load(resp)["roles"]}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # left the guild
        raise


def _role_call(tok: str, method: str, guild_id: int, user_id: int,
               role_id: int) -> None:
    req = urllib.request.Request(
        f"{API}/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
        method=method,
        headers={"Authorization": f"Bot {tok}",
                 "User-Agent": "DiscordBot (dk-scripts, 1.0)",
                 "X-Audit-Log-Reason": "Survivor sim roster carry-over"},
    )
    urllib.request.urlopen(req)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-season", type=int, required=True)
    parser.add_argument("--to-season", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="write changes")
    args = parser.parse_args()

    with open_db_immediate(DB_PATH) as conn:
        target = svc.get_season(conn, args.to_season)
        if target is None:
            sys.exit(f"season {args.to_season} not found")
        if target["status"] == "complete":
            sys.exit(f"season {args.to_season} is complete — wrong target?")
        if int(target["config"]["buyin_coins"]) != 0:
            sys.exit(
                "target season has a nonzero buy-in — bulk enrollment would "
                "debit wallets silently; refusing. Enroll by hand or zero "
                "the buy-in first."
            )
        players = conn.execute(
            "SELECT user_id, status FROM survivor_players WHERE season_id = ?",
            (args.from_season,),
        ).fetchall()
        if not players:
            sys.exit(f"season {args.from_season} has no players")

        names = get_known_user_names_bulk(
            conn, int(target["guild_id"]), [int(p["user_id"]) for p in players]
        )
        enrolled = existing = 0
        for p in players:
            label = names.get(int(p["user_id"])) or p["user_id"]
            if args.apply:
                fresh = svc.add_player(
                    conn, target, int(p["user_id"]), joined_at=time.time()
                )
            else:
                fresh = conn.execute(
                    "SELECT 1 FROM survivor_players WHERE season_id = ? "
                    "AND user_id = ?", (args.to_season, int(p["user_id"])),
                ).fetchone() is None
            if fresh:
                enrolled += 1
                print(f"enroll {label} (was {p['status']} in sim)")
            else:
                existing += 1
                print(f"skip   {label} — already enrolled")

        guild_id = int(target["guild_id"])
        role_survivor = int(target["config"]["role_survivor_id"] or 0)
        role_ghost = int(target["config"]["role_ghost_id"] or 0)

    # Roles outside the DB transaction — REST calls shouldn't hold the lock.
    if not role_survivor:
        print("no survivor role configured — skipping role fixes")
        role_actions = 0
    else:
        tok = _token()
        role_actions = 0
        for p in players:
            label = names.get(int(p["user_id"])) or p["user_id"]
            held = _member_roles(tok, guild_id, int(p["user_id"]))
            if held is None:
                print(f"roles  {label}: not in guild — skipped")
                continue
            wants_add = role_survivor not in held
            wants_drop = bool(role_ghost) and role_ghost in held
            if wants_add:
                print(f"roles  {label}: + Survivor")
                if args.apply:
                    _role_call(tok, "PUT", guild_id, int(p["user_id"]),
                               role_survivor)
                role_actions += 1
            if wants_drop:
                print(f"roles  {label}: - Ghost")
                if args.apply:
                    _role_call(tok, "DELETE", guild_id, int(p["user_id"]),
                               role_ghost)
                role_actions += 1
            time.sleep(0.3)

    verb = "did" if args.apply else "would do"
    print(f"\n{'enrolled' if args.apply else 'would enroll'} {enrolled}, "
          f"{existing} already in; {verb} {role_actions} role change(s)."
          + ("" if args.apply else "  (dry run — use --apply)"))


if __name__ == "__main__":
    main()
