"""One-off backfill for Cat Bot catches that the escaped-username bug never paid.

Cat Bot markdown-escapes usernames (``tryingnewthingz\\_0504``); until the
parser learned to unescape them, `guild.get_member_named` never matched and
every catch by a member whose username contains an underscore silently paid
nobody. Those catches are all still banked in ``games_external_messages`` and,
crucially, have **no claim row** in ``games_external_payouts`` — so they can be
replayed exactly once, through the same ledger the live payout uses.

Usage (dry run first — it writes nothing without --apply):

    python -m scripts.backfill_cat_catches
    python -m scripts.backfill_cat_catches --apply

Semantics, matching ``pay_cat_catch`` as closely as an offline replay can:

* **Claim first.** Each catch reserves its ``games_external_payouts`` row before
  crediting, in the same transaction — so a re-run, or the live bot re-capturing
  an edited message, can never double-pay.
* **Quest triggers fire on the catch's own local day**, not today's. A daily
  quest board from 2026-07-22 is closed, so replaying that day's catches credits
  history without inflating today's board. The per-catch occurrence key
  (``catbot:{message_id}``) is the same one the live path uses.
* **No booster multiplier.** Booster status is a live-gateway fact this script
  can't see, and guessing it wrong overpays; catches are credited at face value.
  Undercrediting a booster by the multiplier is the deliberate trade.
* **Members are resolved from ``known_users``** (username, then display name —
  the two things `get_member_named` matches), restricted to non-bot rows. An
  unresolvable catcher is reported and skipped, never guessed at.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import get_tz_offset_hours, open_db  # noqa: E402
from bot_modules.economy.logic import local_day_for  # noqa: E402
from bot_modules.games_external import parser  # noqa: E402
from bot_modules.services.economy_quests_service import (  # noqa: E402
    fire_trigger_quests,
)
from bot_modules.services.economy_service import (  # noqa: E402
    apply_credit,
    load_econ_settings,
)

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill-cats")


def _resolve_map(conn: sqlite3.Connection, guild_id: int) -> dict[str, int]:
    """``{name: user_id}`` over usernames and display names of non-bot members —
    the same two fields ``discord.Guild.get_member_named`` matches on."""
    out: dict[str, int] = {}
    rows = conn.execute(
        "SELECT user_id, username, display_name FROM known_users "
        "WHERE guild_id = ? AND COALESCE(is_bot, 0) = 0",
        (guild_id,),
    ).fetchall()
    for uid, username, display in rows:
        for name in (display, username):  # username wins on a collision
            if name:
                out[str(name)] = int(uid)
    return out


def _live_since(conn: sqlite3.Connection, guild_id: int) -> str | None:
    """Timestamp of the earliest catch that actually paid — i.e. when the Cat Bot
    payout path went live for this guild.

    Catches banked before it are unpaid because the feature didn't exist yet, not
    because of the escaped-username bug, so they're out of scope for this
    backfill and must not be swept in.
    """
    row = conn.execute(
        "SELECT MIN(m.created_at) FROM games_external_messages m "
        "JOIN games_external_payouts p ON p.message_id = m.message_id "
        "WHERE p.kind = 'catbot' AND m.guild_id = ?",
        (guild_id,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _unpaid_catches(
    conn: sqlite3.Connection, guild_id: int, bot_user_id: int, since: str | None
):
    """Every banked catch with no ``catbot`` payout claim, oldest first."""
    rows = conn.execute(
        """
        SELECT m.message_id, m.created_at, m.content
          FROM games_external_messages m
          LEFT JOIN games_external_payouts p
                 ON p.message_id = m.message_id AND p.kind = 'catbot'
         WHERE m.guild_id = ? AND m.author_id = ? AND p.message_id IS NULL
           AND (? IS NULL OR m.created_at >= ?)
         ORDER BY m.created_at
        """,
        (guild_id, bot_user_id, since, since),
    ).fetchall()
    for message_id, created_at, content in rows:
        catch = parser.parse_cat_catch(content or "")
        if catch is not None:
            yield int(message_id), str(created_at), catch


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument(
        "--since",
        help="ISO timestamp cutoff; defaults to when the payout path went live "
        "for the guild, so pre-launch catches aren't swept in.",
    )
    args = ap.parse_args()

    with open_db(args.db) as conn:
        watches = conn.execute(
            "SELECT guild_id, bot_user_id FROM games_external_watch WHERE kind = 'catbot'"
        ).fetchall()
        if not watches:
            log.error("No catbot watch configured — nothing to back-fill.")
            return 1

        total_coins = 0
        per_user: Counter[int] = Counter()
        catches: Counter[int] = Counter()
        unresolved: Counter[str] = Counter()

        for guild_id, bot_user_id in watches:
            guild_id, bot_user_id = int(guild_id), int(bot_user_id)
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                log.warning("Economy disabled for guild %s — skipping.", guild_id)
                continue
            names = _resolve_map(conn, guild_id)
            offset = get_tz_offset_hours(conn, guild_id)
            since = args.since or _live_since(conn, guild_id)
            log.info("Guild %s: replaying unpaid catches from %s", guild_id, since)

            for message_id, created_at, catch in _unpaid_catches(
                conn, guild_id, bot_user_id, since
            ):
                user_id = names.get(catch.username)
                if user_id is None:
                    unresolved[catch.username] += 1
                    continue

                if not args.apply:
                    total_coins += catch.coins
                    per_user[user_id] += catch.coins
                    catches[user_id] += 1
                    continue

                # Claim first, in the same transaction as the credit: a row here
                # is the one-time guarantee the live payout path relies on too.
                cur = conn.execute(
                    "INSERT OR IGNORE INTO games_external_payouts "
                    "(message_id, guild_id, kind) VALUES (?, ?, 'catbot')",
                    (message_id, guild_id),
                )
                if cur.rowcount == 0:
                    continue  # raced/duplicate — someone already paid it

                apply_credit(
                    conn, guild_id, user_id, catch.coins, "cat_catch",
                    meta={
                        "rarity": catch.rarity,
                        "doubled": catch.doubled,
                        "backfill": "escaped-username",
                    },
                    booster=False,
                )
                # The catch's own local day, so a closed daily board stays closed.
                ts = datetime.fromisoformat(created_at).timestamp()
                fire_trigger_quests(
                    conn, settings, guild_id, "cat_catch", user_id,
                    local_day=local_day_for(ts, offset),
                    occurrence=f"catbot:{message_id}",
                    booster=False,
                )
                total_coins += catch.coins
                per_user[user_id] += catch.coins
                catches[user_id] += 1

        verb = "Credited" if args.apply else "Would credit"
        for uid, coins in per_user.most_common():
            log.info("  %s %-20s %4d catches  %5d coins", verb, uid, catches[uid], coins)
        log.info("%s %d coins across %d members", verb, total_coins, len(per_user))
        for name, n in unresolved.most_common():
            log.warning("  unresolved catcher %r — %d catches skipped", name, n)
        if not args.apply:
            log.info("Dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
