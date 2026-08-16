"""One-off replay for finished Gamebot games that were never paid.

Gamebot's results were banked from 2026-07-04 onward, but the payout path only
went live on 2026-07-26 — and even then it mis-identified two of its three
sub-games. Every unpaid game is still sitting in ``games_external_messages``
with **no claim row** in ``games_external_payouts``, so it can be replayed
exactly once through the same ledger the live payout uses.

Usage (dry run first — it writes nothing without --apply):

    python -m scripts.replay_gamebot_games
    python -m scripts.replay_gamebot_games --apply

Take a DB backup before --apply.

The replay is driven off the *Game over!* embeds and feeds each one through
``current_game_window`` → ``identify_game`` → the matching extractor, i.e. the
identical functions ``GamesExternalCog._pay_gamebot_game`` calls live. Backfill
and live payout therefore cannot diverge: there is one parser, and this script
is a second caller of it, not a second implementation.

Semantics, matching the live payout as closely as an offline replay can:

* **Claim first.** Each game reserves its ``games_external_payouts`` row before
  crediting, in the same transaction. ``message_id`` is that table's PRIMARY
  KEY, so double-payment is structurally impossible — the game already paid
  live on 2026-07-26 is skipped automatically, with no cutoff to get wrong.
* **Quest triggers fire on the game's own local day**, not today's. A daily
  board from 2026-07-14 is closed, so replaying it credits history without
  dumping three weeks of backlog onto today's board at once. Same choice, for
  the same reason, as ``backfill_cat_catches.py``.
* **No booster multiplier.** Booster status is a live-gateway fact this script
  can't see, and guessing it wrong overpays. Undercrediting a booster is the
  deliberate trade.
* **Members are resolved from ``known_users``**, restricted to non-bot rows —
  by id for CAH/Connect 4 (which use mentions) and by username/display name for
  Anagrams (whose scoreboard prints usernames, like Cat Bot). Anyone
  unresolvable is reported and skipped, never guessed at.
* **Abandoned lobbies pay nobody** but are still claimed, so they're never
  reconsidered. Sub-games with no parser (Chess, Poker, …) are left unclaimed
  so that teaching the parser one later can replay them.
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
log = logging.getLogger("replay-gamebot")

# Ledger `kind` per sub-game — the same values the cog writes.
_CLAIM_KIND = {
    parser.GAME_CAH: "gamebot_cah",
    parser.GAME_CONNECT4: "gamebot_connect4",
    parser.GAME_ANAGRAMS: "gamebot_anagrams",
}

# Quest occurrence namespace per sub-game, matching what the live payout passes
# to _fire_triggers (``game_key`` for the score-based games, ``game_type`` for
# the flat one). Keeping these identical is what makes replaying a game the
# live path already paid a no-op at the quest layer as well as at the ledger.
_TRIGGER_SCOPE = {
    parser.GAME_CAH: "cah",
    parser.GAME_ANAGRAMS: "anagrams",
    parser.GAME_CONNECT4: "connect4",
}


def _members(conn: sqlite3.Connection, guild_id: int) -> tuple[set[int], dict[str, int]]:
    """(non-bot member ids, ``{name: user_id}``) for a guild.

    The name map covers usernames and display names — the two fields
    ``discord.Guild.get_member_named`` matches on — for the Anagrams scoreboard.
    """
    ids: set[int] = set()
    by_name: dict[str, int] = {}
    rows = conn.execute(
        "SELECT user_id, username, display_name FROM known_users "
        "WHERE guild_id = ? AND COALESCE(is_bot, 0) = 0",
        (guild_id,),
    ).fetchall()
    for uid, username, display in rows:
        ids.add(int(uid))
        for name in (display, username):  # username wins on a collision
            if name:
                by_name[str(name)] = int(uid)
    return ids, by_name


def _unpaid_games(conn: sqlite3.Connection, guild_id: int, bot_user_id: int, channel_id: int):
    """Yield (message_id, created_at, window) for each unclaimed finished game
    in one watched channel, oldest first.

    Windows are built from the channel's *whole* banked history so a game whose
    lobby predates the first unpaid game still resolves; only the terminal
    messages without a payout claim are yielded.
    """
    rows = conn.execute(
        """
        SELECT m.message_id, m.created_at, m.embeds_json,
               p.message_id IS NOT NULL AS paid
          FROM games_external_messages m
          LEFT JOIN games_external_payouts p ON p.message_id = m.message_id
         WHERE m.guild_id = ? AND m.author_id = ? AND m.channel_id = ?
         ORDER BY m.created_at
        """,
        (guild_id, bot_user_id, channel_id),
    ).fetchall()
    parsed = [{"embeds": json.loads(r[2] or "[]")} for r in rows]
    for i, row in enumerate(rows):
        if not parser.is_terminal(parsed[i]["embeds"]):
            continue
        if row[3]:  # already claimed — live payout, or an earlier --apply run
            continue
        yield int(row[0]), str(row[1]), parser.current_game_window(parsed, i)


def _cah_shares(scores: dict[int, int], cap: int) -> dict[int, int]:
    """Coins per player, mirroring ``pay_cah_game_by_score``: the top scorer
    earns ``cap`` and everyone else that scaled by their score ratio, rounded.
    A share under 1 pays nothing (``apply_credit`` rejects it)."""
    top = max(scores.values()) if scores else 0
    if top <= 0:
        return {}
    out = {}
    for uid, score in scores.items():
        share = round(cap * score / top)
        if share >= 1:
            out[uid] = share
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    with open_db(args.db) as conn:
        watches = conn.execute(
            "SELECT guild_id, bot_user_id, channel_id FROM games_external_watch "
            "WHERE kind = 'gamebot'"
        ).fetchall()
        if not watches:
            log.error("No gamebot watch configured — nothing to replay.")
            return 1

        per_user: Counter[int] = Counter()
        games: Counter[str] = Counter()
        unresolved: Counter[str] = Counter()
        total = 0

        for guild_id, bot_user_id, channel_id in watches:
            guild_id, bot_user_id = int(guild_id), int(bot_user_id)
            channel_id = int(channel_id)
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                log.warning("Economy disabled for guild %s — skipping.", guild_id)
                continue
            member_ids, by_name = _members(conn, guild_id)
            offset = get_tz_offset_hours(conn, guild_id)
            log.info("Guild %s channel %s: replaying unpaid games", guild_id, channel_id)

            for message_id, created_at, window in _unpaid_games(
                conn, guild_id, bot_user_id, channel_id
            ):
                game = parser.identify_game(window)
                when = created_at[:16]

                if parser.is_abandoned(window):
                    games["abandoned"] += 1
                    log.info("  %s  abandoned lobby — pays nobody", when)
                    if args.apply:
                        conn.execute(
                            "INSERT OR IGNORE INTO games_external_payouts "
                            "(message_id, guild_id, kind) VALUES (?, ?, ?)",
                            (message_id, guild_id, "gamebot_abandoned"),
                        )
                    continue

                if game is None:
                    games["unparsed"] += 1
                    log.info("  %s  no parser for this game — left unclaimed", when)
                    continue

                # ── work out who is owed what, per sub-game ──────────────────
                #
                # A set, because CAH can finish with the lead tied since
                # Gamebot stopped declaring a winner (2026-08-15) — see
                # parser.extract_cah_game. Connect 4 and Anagrams still name
                # exactly one, so theirs is a set of one or none.
                winners: set[int]
                if game == parser.GAME_CAH:
                    raw_scores, cah_winners = parser.extract_cah_game(window)
                    winners = set(cah_winners)
                    scores = {u: s for u, s in raw_scores.items() if u in member_ids}
                    payouts = _cah_shares(scores, settings.reward_cah_win_max)
                    roster = sorted(scores)
                elif game == parser.GAME_ANAGRAMS:
                    named, winner = parser.extract_anagrams_game(window)
                    winners = {winner} if winner is not None else set()
                    scores = {}
                    for name, points in named.items():
                        uid = by_name.get(name)
                        if uid is None:
                            unresolved[name] += 1
                            continue
                        scores[uid] = max(scores.get(uid, 0), points)
                    if winner is not None and winner in member_ids:
                        scores.setdefault(winner, 0)
                    payouts = _cah_shares(scores, settings.reward_cah_win_max)
                    roster = sorted(scores)
                else:  # Connect 4 — flat participation + win bonus, no scores
                    raw_roster, winner = parser.extract_connect4_game(window)
                    winners = {winner} if winner is not None else set()
                    scores = {}
                    roster = sorted(u for u in raw_roster if u in member_ids)
                    payouts = {u: settings.reward_game_participation for u in roster}
                    if winner in roster and settings.reward_game_win > 0:
                        payouts[winner] = payouts[winner] + settings.reward_game_win

                # Left the server / a bot — no win bonus.
                winners &= member_ids
                if not roster:
                    log.info("  %s  %-8s no resolvable players — skipped", when, game)
                    continue

                games[game] += 1
                total += sum(payouts.values())
                for uid, coins in payouts.items():
                    per_user[uid] += coins
                log.info(
                    "  %s  %-8s %2d players, %4d coins, winner(s) %s",
                    when, game, len(roster), sum(payouts.values()),
                    sorted(winners) or "none",
                )

                if not args.apply:
                    continue

                # Claim first, in the same transaction as the credits: the row
                # here is the one-time guarantee the live path relies on too.
                cur = conn.execute(
                    "INSERT OR IGNORE INTO games_external_payouts "
                    "(message_id, guild_id, kind) VALUES (?, ?, ?)",
                    (message_id, guild_id, _CLAIM_KIND[game]),
                )
                if cur.rowcount == 0:
                    continue  # raced — someone already paid it

                day = local_day_for(datetime.fromisoformat(created_at).timestamp(), offset)
                top = max(scores.values()) if game != parser.GAME_CONNECT4 and scores else 0
                for uid in roster:
                    coins = payouts.get(uid, 0)
                    if coins >= 1:
                        meta = (
                            {"score": scores[uid], "top_score": top, "backfill": game}
                            if game != parser.GAME_CONNECT4
                            else {"backfill": game}
                        )
                        apply_credit(
                            conn, guild_id, uid, coins,
                            "game_win" if uid in winners else "game_participation",
                            meta=meta, booster=False,
                        )
                    # Quest triggers fire for the whole roster, including anyone
                    # whose coin share rounded to nothing — they still played.
                    occurrence = f"{_TRIGGER_SCOPE[game]}:{message_id}"
                    fire_trigger_quests(
                        conn, settings, guild_id, "party_game", uid,
                        local_day=day, occurrence=occurrence, booster=False,
                    )
                    if uid in winners:
                        fire_trigger_quests(
                            conn, settings, guild_id, "game_win", uid,
                            local_day=day, occurrence=occurrence, booster=False,
                        )

        verb = "Credited" if args.apply else "Would credit"
        log.info("")
        for uid, coins in per_user.most_common():
            log.info("  %s %-20s %5d coins", verb, uid, coins)
        log.info(
            "%s %d coins across %d members — %s",
            verb, total, len(per_user),
            ", ".join(f"{n} {g}" for g, n in sorted(games.items())) or "no games",
        )
        for name, n in unresolved.most_common():
            log.warning("  unresolved Anagrams player %r — %d game(s) skipped", name, n)
        if not args.apply:
            log.info("Dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
