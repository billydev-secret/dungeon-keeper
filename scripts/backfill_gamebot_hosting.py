"""One-off backfill of the host bounty for external Gamebot games.

Native party games have always paid their host (``award_host_bounty`` +
the ``game_host`` trigger), but the external-game payout never passed a host —
so every Gamebot game ever tracked paid its players and nothing to whoever
started it. Gamebot does name the host, in the lobby embed's title
("efficientpanic is starting a Cards Against Humanity game!"), so the whole
backlog is recoverable.

Usage (dry run first — it writes nothing without --apply):

    python -m scripts.backfill_gamebot_hosting
    python -m scripts.backfill_gamebot_hosting --apply
    python -m scripts.backfill_gamebot_hosting --apply --boosters 123,456

Take a DB backup before --apply.

This is **independent of** ``scripts/replay_gamebot_games.py``. That one pays
participation/win off the ``games_external_payouts`` ledger; this pays hosting,
a separate faucet that ledger has no room for — its key is the Game-over
message id, and a game whose *participation* has already been paid still owes
its host. Idempotency here comes from the ledger entry itself: each credit is
stamped ``meta.game`` with the Game-over message id, and a game already
carrying a ``game_host`` row for that id is skipped. Running both scripts is
safe in either order.

Semantics, matching ``pay_host_bounty`` as closely as an offline replay can:

* **The anti-farm gate is preserved.** ``joiners`` counts players other than
  the host, so a game nobody else joined pays nothing — which is also what
  makes both abandoned lobbies in the history pay nothing.
* **Quest triggers fire on the game's own local day**, not today's, and use the
  live path's ``game_host:<game-over-message-id>`` occurrence key, so a game
  already credited by the live bot is a no-op at the quest layer too.
* **Booster status must be supplied explicitly** via ``--boosters``. It lives
  only on the Discord gateway (``member.premium_since``) and is recorded
  nowhere in the database, so an offline script cannot look it up. Anyone not
  listed is credited flat. Passing the wrong ids overpays, so the default is
  nobody.
* **Hosts are resolved from ``known_users``** (username, then display name —
  what ``get_member_named`` matches), restricted to non-bot rows. An
  unresolvable host is reported and skipped, never guessed at.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import get_tz_offset_hours, open_db  # noqa: E402
from bot_modules.economy.logic import host_bounty_amount, local_day_for  # noqa: E402
from bot_modules.games_external import parser  # noqa: E402
from bot_modules.services.economy_quests_service import (  # noqa: E402
    fire_trigger_quests,
    source_enabled,
)
from bot_modules.services.economy_service import (  # noqa: E402
    apply_credit,
    load_econ_settings,
)

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill-hosting")


def _members(conn: sqlite3.Connection, guild_id: int) -> tuple[set[int], dict[str, int]]:
    """(non-bot member ids, ``{name: user_id}``) for a guild."""
    ids: set[int] = set()
    by_name: dict[str, int] = {}
    for uid, username, display in conn.execute(
        "SELECT user_id, username, display_name FROM known_users "
        "WHERE guild_id = ? AND COALESCE(is_bot, 0) = 0",
        (guild_id,),
    ):
        ids.add(int(uid))
        for name in (display, username):  # username wins on a collision
            if name:
                by_name[str(name)] = int(uid)
    return ids, by_name


def _already_hosted(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    """Game-over message ids that already carry a ``game_host`` credit.

    The stamp this reads is written by ``--apply`` below; the live path doesn't
    set it, so a game the bot paid hosting for after this script shipped would
    be re-credited. That window is the live fix's own deploy — by design this
    is meant to be run once, against the pre-fix backlog.
    """
    return {
        int(r[0])
        for r in conn.execute(
            "SELECT json_extract(meta, '$.game') FROM econ_ledger "
            "WHERE guild_id = ? AND kind = 'game_host' "
            "AND json_extract(meta, '$.game') IS NOT NULL",
            (guild_id,),
        )
    }


def _roster(window, game: str, by_name: dict[str, int], member_ids: set[int]) -> set[int]:
    """Who actually played this game — the joiner count the bounty scales on."""
    if game == parser.GAME_CAH:
        roster = set(parser.extract_cah_game(window)[0])
    elif game == parser.GAME_CONNECT4:
        roster = set(parser.extract_connect4_game(window)[0])
    elif game == parser.GAME_ANAGRAMS:
        roster = {
            by_name[n] for n in parser.extract_anagrams_game(window)[0] if n in by_name
        }
    else:
        roster = set()
    return {u for u in roster if u in member_ids}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument(
        "--boosters", default="",
        help="comma-separated user ids currently boosting; they get the "
        "multiplier. Not discoverable offline, so nobody gets it by default.",
    )
    args = ap.parse_args()
    boosters = {int(x) for x in args.boosters.replace(" ", "").split(",") if x}

    with open_db(args.db) as conn:
        watches = conn.execute(
            "SELECT guild_id, bot_user_id, channel_id FROM games_external_watch "
            "WHERE kind = 'gamebot'"
        ).fetchall()
        if not watches:
            log.error("No gamebot watch configured — nothing to back-fill.")
            return 1

        per_host: Counter[int] = Counter()
        hosted: Counter[int] = Counter()
        skipped: Counter[str] = Counter()
        unresolved: Counter[str] = Counter()
        total = 0

        for guild_id, bot_user_id, channel_id in watches:
            guild_id, bot_user_id = int(guild_id), int(bot_user_id)
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                log.warning("Economy disabled for guild %s — skipping.", guild_id)
                continue
            if not source_enabled(conn, guild_id, "game_host"):
                log.warning("game_host income source is off for guild %s — skipping.", guild_id)
                continue
            member_ids, by_name = _members(conn, guild_id)
            offset = get_tz_offset_hours(conn, guild_id)
            done = _already_hosted(conn, guild_id)

            rows = conn.execute(
                "SELECT message_id, created_at, embeds_json FROM games_external_messages "
                "WHERE guild_id = ? AND author_id = ? AND channel_id = ? ORDER BY created_at",
                (guild_id, bot_user_id, int(channel_id)),
            ).fetchall()
            parsed = [{"embeds": json.loads(r[2] or "[]")} for r in rows]

            for i, row in enumerate(rows):
                if not parser.is_terminal(parsed[i]["embeds"]):
                    continue
                message_id, created_at = int(row[0]), str(row[1])
                if message_id in done:
                    skipped["already credited"] += 1
                    continue
                window = parser.current_game_window(parsed, i)
                when = created_at[:16]

                if parser.is_abandoned(window):
                    skipped["abandoned lobby"] += 1
                    continue
                game = parser.identify_game(window)
                if game is None:
                    skipped["no parser for game"] += 1
                    continue

                host_name = parser.host_from_window(window)
                host_id = by_name.get(host_name) if host_name else None
                if host_id is None:
                    unresolved[host_name or "(no lobby embed)"] += 1
                    continue

                roster = _roster(window, game, by_name, member_ids)
                joiners = len(roster - {host_id})
                # The anti-farm gate: a game nobody else joined pays nothing.
                coins = host_bounty_amount(
                    joiners, settings.host_bounty_per_joiner, settings.host_bounty_cap
                )
                if coins <= 0:
                    skipped["no joiners (anti-farm gate)"] += 1
                    continue
                booster = host_id in boosters
                if booster:
                    coins = int(coins * settings.booster_multiplier)

                log.info(
                    "  %s  %-8s host %-18s %d joiner(s)  %4d coins%s",
                    when, game, host_name, joiners, coins,
                    " (booster)" if booster else "",
                )
                per_host[host_id] += coins
                hosted[host_id] += 1
                total += coins

                if not args.apply:
                    continue

                apply_credit(
                    conn, guild_id, host_id, coins, "game_host",
                    meta={"joiners": joiners, "game": message_id, "backfill": game},
                    booster=False,  # already folded into `coins` above
                )
                # The game's own local day, so a closed daily board stays closed,
                # and the live path's occurrence key so a re-run is a no-op.
                ts = datetime.fromisoformat(created_at).timestamp()
                fire_trigger_quests(
                    conn, settings, guild_id, "game_host", host_id,
                    local_day=local_day_for(ts, offset),
                    occurrence=str(message_id), booster=booster,
                )

        verb = "Credited" if args.apply else "Would credit"
        log.info("")
        names = {}
        for gid, _b, _c in watches:
            names.update(
                {v: k for k, v in _members(conn, int(gid))[1].items()}
            )
        for uid, coins in per_host.most_common():
            log.info(
                "  %s %-20s %5d coins (%d game(s))",
                verb, names.get(uid, uid), coins, hosted[uid],
            )
        log.info("%s %d coins across %d host(s)", verb, total, len(per_host))
        for reason, n in skipped.most_common():
            log.info("  skipped %d — %s", n, reason)
        for name, n in unresolved.most_common():
            log.warning("  unresolved host %r — %d game(s) skipped", name, n)
        if not args.apply:
            log.info("Dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
