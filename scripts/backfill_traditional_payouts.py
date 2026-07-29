"""One-off backfill for Truth or Dare games the expiry sweep never paid.

Truth or Dare shipped without an End Game button, so ``_do_close`` — the only
call site passing ``bot=``/``player_ids=`` to ``end_game``, i.e. the only one
that pays — was unreachable. Every game was instead reaped by the 24-hour sweep,
which archived with a bare ``end_game``: ``player_count = 0``, ``payload = {}``,
nobody credited. All 18 traditional games in the history are in that state.

The roster is still recoverable, by user id rather than by name. Asking a
question posts the target's ``<@id>`` mention into the channel (the modal's
``content=mention``), and those bot messages are retained in ``messages`` — so
the distinct mentions inside a game's window are exactly its participants. For
game 9d55761a (2026-07-28) that yields 16 ids, matching the live embed's
"16 PARTICIPANTS / 17 of 17".

Usage (dry run first — it writes nothing without --apply):

    python -m scripts.backfill_traditional_payouts
    python -m scripts.backfill_traditional_payouts --apply
    python -m scripts.backfill_traditional_payouts --apply --boosters 123,456

Take a DB backup before --apply.

Semantics, matching ``pay_game_rewards`` as closely as an offline replay can:

* **Claim first, exactly once.** Each credit is stamped ``meta.game`` with the
  game's uuid; a game already carrying a ``game_participation`` row for that id
  is skipped, so a re-run — or a game the live bot paid after the fix deploys —
  is a no-op.
* **Concurrent games in one channel are paid once, jointly.** Games 99/100 and
  128/129 were launched seconds apart in the same channel and reaped in the same
  sweep, so their ~24h windows overlap almost entirely and no rule of time can
  say which mention belongs to which game. Attributing per-game would pay one
  population twice. Overlapping same-channel games are therefore merged into one
  cluster, paid once under the earliest game's id. All such clusters in the
  current history share a single host, so no host bounty is ambiguous; if that
  ever stops being true the script refuses the cluster rather than guessing.
* **The anti-farm gate is preserved.** Host bounty scales on joiners *other*
  than the host, so a lobby nobody joined pays nothing — which is what the three
  empty games in the history do.
* **Traditional has no winner.** ``resolve_winners('traditional', …)`` is empty
  by design, so this credits participation and hosting only, never a win bonus.
* **Quest triggers fire on the game's own local day**, not today's, using the
  live path's ``traditional:<game_id>`` occurrence key — so a closed daily board
  stays closed and an already-credited game is a no-op at the quest layer too.
* **Booster status must be supplied explicitly** via ``--boosters``. It lives
  only on the Discord gateway (``member.premium_since``) and is recorded nowhere
  in the database, so an offline script cannot look it up. Anyone not listed is
  credited flat; passing the wrong ids overpays, so the default is nobody.
* **Members who have since left are skipped and reported**, never credited —
  the live path resolves participants through ``guild.get_member``, which
  returns nothing for a departed member.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import get_tz_offset_hours, open_db  # noqa: E402
from bot_modules.economy.logic import host_bounty_amount, local_day_for  # noqa: E402
from bot_modules.services.economy_quests_service import (  # noqa: E402
    fire_trigger_quests,
    source_enabled,
)
from bot_modules.services.economy_service import (  # noqa: E402
    apply_credit,
    load_econ_settings,
)

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"
GAME_TYPE = "traditional"
MENTION_RE = re.compile(r"^<@!?(\d+)>$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill-tod")


class Cluster:
    """One or more same-channel games whose windows overlap, paid as a unit."""

    def __init__(self, row: sqlite3.Row):
        self.game_ids = [str(row["game_id"])]
        self.history_ids = [int(row["history_id"])]
        self.channel_id = int(row["channel_id"])
        self.host_ids = {int(row["host_id"])}
        self.started_at = str(row["started_at"])
        self.ended_at = str(row["ended_at"])

    def absorb(self, row: sqlite3.Row) -> None:
        self.game_ids.append(str(row["game_id"]))
        self.history_ids.append(int(row["history_id"]))
        self.host_ids.add(int(row["host_id"]))
        self.ended_at = max(self.ended_at, str(row["ended_at"]))

    def overlaps(self, row: sqlite3.Row) -> bool:
        return (
            int(row["channel_id"]) == self.channel_id
            and str(row["started_at"]) <= self.ended_at
        )

    @property
    def label(self) -> str:
        return "+".join(f"#{h}" for h in self.history_ids)


def _cluster(rows: list[sqlite3.Row]) -> list[Cluster]:
    """Merge same-channel games with overlapping windows into payout units."""
    per_channel: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        per_channel.setdefault(int(row["channel_id"]), []).append(row)

    clusters: list[Cluster] = []
    for channel_rows in per_channel.values():
        current: Cluster | None = None
        for row in sorted(channel_rows, key=lambda r: str(r["started_at"])):
            if current is not None and current.overlaps(row):
                current.absorb(row)
                continue
            current = Cluster(row)
            clusters.append(current)
    return sorted(clusters, key=lambda c: c.started_at)


def _members(conn: sqlite3.Connection, guild_id: int) -> tuple[set[int], dict[int, str]]:
    """(currently-present non-bot member ids, ``{user_id: name}``)."""
    ids: set[int] = set()
    names: dict[int, str] = {}
    for uid, username, display, current in conn.execute(
        "SELECT user_id, username, display_name, COALESCE(current_member, 1) "
        "FROM known_users WHERE guild_id = ? AND COALESCE(is_bot, 0) = 0",
        (guild_id,),
    ):
        names[int(uid)] = str(display or username or uid)
        if current:
            ids.add(int(uid))
    return ids, names


def _bot_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    return {
        int(r[0])
        for r in conn.execute(
            "SELECT user_id FROM known_users WHERE guild_id = ? AND COALESCE(is_bot, 0) = 1",
            (guild_id,),
        )
    }


def _already_paid(conn: sqlite3.Connection, guild_id: int) -> set[str]:
    """Game ids that already carry a backfilled participation credit."""
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT json_extract(meta, '$.game') FROM econ_ledger "
            "WHERE guild_id = ? AND kind = 'game_participation' "
            "AND json_extract(meta, '$.game') IS NOT NULL",
            (guild_id,),
        )
    }


def _roster(
    conn: sqlite3.Connection, cluster: Cluster, bot_ids: set[int]
) -> list[int]:
    """Distinct users the bot pinged inside the cluster's window — the roster.

    Only bare-mention messages count: that is the shape ``AskQuestionModal`` and
    the bank round post (``content=mention``), so a bot message that merely
    happens to contain a mention can't inflate the roster.
    """
    if not bot_ids:
        return []
    placeholders = ",".join("?" for _ in bot_ids)
    rows = conn.execute(
        f"SELECT content FROM messages WHERE channel_id = ? "  # noqa: S608 - ids are ints
        f"AND author_id IN ({placeholders}) "
        "AND ts BETWEEN strftime('%s', ?) AND strftime('%s', ?) "
        "AND content LIKE '<@%'",
        (cluster.channel_id, *sorted(bot_ids), cluster.started_at, cluster.ended_at),
    ).fetchall()

    roster: list[int] = []
    for (content,) in rows:
        match = MENTION_RE.match(str(content or "").strip())
        if match is None:
            continue
        uid = int(match.group(1))
        if uid not in roster and uid not in bot_ids:
            roster.append(uid)
    return roster


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
        conn.row_factory = sqlite3.Row
        guild_row = conn.execute(
            "SELECT guild_id FROM games_game_config LIMIT 1"
        ).fetchone()
        if guild_row is None:
            log.error("No games guild configured — nothing to back-fill.")
            return 1
        guild_id = int(guild_row["guild_id"])

        settings = load_econ_settings(conn, guild_id)
        if not settings.enabled:
            log.error("Economy disabled for guild %s — nothing to back-fill.", guild_id)
            return 1

        member_ids, names = _members(conn, guild_id)
        bot_ids = _bot_ids(conn, guild_id)
        offset = get_tz_offset_hours(conn, guild_id)
        done = _already_paid(conn, guild_id)
        host_source_on = source_enabled(conn, guild_id, "game_host")
        if not host_source_on:
            log.warning("game_host income source is off — crediting players only.")

        rows = conn.execute(
            "SELECT history_id, game_id, channel_id, host_id, started_at, ended_at "
            "FROM games_game_history WHERE game_type = ? AND player_count = 0 "
            "ORDER BY started_at",
            (GAME_TYPE,),
        ).fetchall()

        per_user: Counter[int] = Counter()
        skipped: Counter[str] = Counter()
        departed: Counter[int] = Counter()
        total = paid_games = 0

        for cluster in _cluster(rows):
            if any(g in done for g in cluster.game_ids):
                skipped["already credited"] += 1
                continue
            if len(cluster.host_ids) > 1:
                # Can't split one merged roster across two hosts' bounties.
                log.warning("  %s spans hosts %s — skipped for manual review",
                            cluster.label, sorted(cluster.host_ids))
                skipped["cluster spans multiple hosts"] += 1
                continue

            host_id = next(iter(cluster.host_ids))
            recovered = _roster(conn, cluster, bot_ids)
            if not recovered:
                skipped["no roster recoverable"] += 1
                log.info("  %-8s %s  no mentions in window — nothing to pay",
                         cluster.label, cluster.started_at[:16])
                continue

            present = [u for u in recovered if u in member_ids]
            for uid in recovered:
                if uid not in member_ids:
                    departed[uid] += 1
            if not present:
                skipped["whole roster has left the guild"] += 1
                continue

            joiners = len({*present} - {host_id})
            bounty = (
                host_bounty_amount(
                    joiners, settings.host_bounty_per_joiner, settings.host_bounty_cap
                )
                if host_source_on
                else 0
            )
            log.info(
                "  %-8s %s  ch %-19s %2d player(s)%s  host %s +%d",
                cluster.label, cluster.started_at[:16], cluster.channel_id,
                len(present),
                f" (+{len(recovered) - len(present)} left)" if len(recovered) > len(present) else "",
                names.get(host_id, host_id), bounty,
            )

            occurrence = f"{GAME_TYPE}:{cluster.game_ids[0]}"
            stamp = {"game": cluster.game_ids[0], "backfill": GAME_TYPE,
                     "games": cluster.game_ids}
            ts = datetime.fromisoformat(cluster.started_at).timestamp()
            local_day = local_day_for(ts, offset)
            paid_games += 1

            for uid in present:
                coins = settings.reward_game_participation
                if uid in boosters:
                    coins = int(coins * settings.booster_multiplier)
                per_user[uid] += coins
                total += coins
                if not args.apply:
                    continue
                apply_credit(
                    conn, guild_id, uid, coins, "game_participation",
                    meta=stamp, booster=False,  # multiplier already folded in
                )
                fire_trigger_quests(
                    conn, settings, guild_id, "party_game", uid,
                    local_day=local_day, occurrence=occurrence,
                    booster=uid in boosters,
                )

            if bounty > 0 and host_id in member_ids:
                if host_id in boosters:
                    bounty = int(bounty * settings.booster_multiplier)
                per_user[host_id] += bounty
                total += bounty
                if args.apply:
                    apply_credit(
                        conn, guild_id, host_id, bounty, "game_host",
                        meta={**stamp, "joiners": joiners}, booster=False,
                    )
                    fire_trigger_quests(
                        conn, settings, guild_id, "game_host", host_id,
                        local_day=local_day, occurrence=occurrence,
                        booster=host_id in boosters,
                    )
            elif bounty > 0:
                skipped["host has left the guild"] += 1

        verb = "Credited" if args.apply else "Would credit"
        log.info("")
        for uid, coins in per_user.most_common():
            log.info("  %s %-24s %5d coins", verb, names.get(uid, uid), coins)
        log.info(
            "%s %d coins to %d member(s) across %d game unit(s)",
            verb, total, len(per_user), paid_games,
        )
        for reason, n in skipped.most_common():
            log.info("  skipped %d — %s", n, reason)
        for uid, n in departed.most_common():
            log.warning("  %s has left the guild — skipped in %d game(s)",
                        names.get(uid, uid), n)
        if not args.apply:
            log.info("Dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
