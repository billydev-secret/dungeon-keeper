"""One-off backfill for Hot Seat turns taken before Mention Awards existed.

**This script does not use the live rules, and cannot.** Mention Awards
matches condition chips (text among them) against message content read live
off the gateway; content is never stored, so there is nothing to re-match
over history.

What *is* stored — and is all this needs — is the announcement's shape:
``media_kind`` (derived from attachment filenames) and the @-mention edges
survive with content storage off. The Hot Seat handoff has an unmistakable
one: a card image plus exactly one mention. Measured over 2026-07-23..08-07
in the live channel, 19 messages carried media, 15 of those mentioned exactly
one user, and all 15 were real announcements — no false positives.

So this replays the historical turns on the *shape* rule while the live
watcher runs on *chips*. That seam is deliberate and is the reason this is a
one-off script rather than a mode of the feature.

Usage (dry run first — it writes nothing without --apply):

    python -m scripts.backfill_mention_awards --channel <id> --amount 250
    python -m scripts.backfill_mention_awards --channel <id> --amount 250 --apply

Semantics, matching the live path as closely as an offline replay can, and
mirroring ``backfill_cat_catches``:

* **Claim first.** Each announcement reserves its ``games_external_payouts``
  row before crediting, in the same transaction, so a re-run — or the live
  watcher later seeing an edit of the same message — can never double-pay.
* **Per-announcement dedupe only**, matching the rule chosen for this feature:
  a member who takes the seat twice is paid twice.
* **Quest triggers fire on the announcement's own local day**, not today's, so
  replaying 2026-07-27 credits history without inflating today's board. The
  occurrence key (``mention_award:{message_id}``) matches the live path, so a
  turn paid here can never be paid again by the watcher.
* **No booster multiplier.** Booster status is a live-gateway fact this script
  can't see, and guessing wrong overpays. Undercrediting a booster is the
  deliberate trade — the same call ``backfill_cat_catches`` made.
* **Self-nominations are skipped**, as in the live matcher.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import get_tz_offset_hours, open_db  # noqa: E402
from bot_modules.economy.logic import local_day_for  # noqa: E402
from bot_modules.games_external.logic import claim_payout_sync  # noqa: E402
from bot_modules.mention_awards.logic import (  # noqa: E402
    PAYOUT_KIND,
    quest_occurrence,
    recipient_of,
)
from bot_modules.services.economy_quests_service import (  # noqa: E402
    fire_trigger_quests,
)
from bot_modules.services.message_store import (  # noqa: E402
    MEDIA_KIND_GIF,
    MEDIA_KIND_MEDIA,
    get_known_user_names_bulk,
)
from bot_modules.services.economy_service import (  # noqa: E402
    apply_credit,
    load_econ_settings,
)

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

# The same vocabulary ``message_store.classify_media_kind`` writes.
CARD_MEDIA_KINDS = (MEDIA_KIND_MEDIA, MEDIA_KIND_GIF)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill-mention-awards")


def _announcements(conn: sqlite3.Connection, guild_id: int, channel_id: int):
    """Unpaid handoffs in one channel, oldest first: (message_id, ts, member, announcer).

    A message qualifies on shape alone: posted by a non-bot, carries a card
    image, and @-mentions exactly one member who isn't the author.
    """
    rows = conn.execute(
        f"""
        SELECT m.message_id, m.ts, m.author_id
          FROM messages m
          LEFT JOIN known_users k
                 ON k.user_id = m.author_id AND k.guild_id = m.guild_id
          LEFT JOIN games_external_payouts p
                 ON p.message_id = m.message_id
         WHERE m.guild_id = ? AND m.channel_id = ?
           AND m.media_kind IN ({','.join('?' * len(CARD_MEDIA_KINDS))})
           AND COALESCE(k.is_bot, 0) = 0
           AND p.message_id IS NULL
         ORDER BY m.ts
        """,
        (guild_id, channel_id, *CARD_MEDIA_KINDS),
    ).fetchall()

    for message_id, ts, author_id in rows:
        mentions = [
            int(r[0])
            for r in conn.execute(
                "SELECT user_id FROM message_mentions WHERE message_id = ?",
                (int(message_id),),
            ).fetchall()
        ]
        # Same recipient rule as the live matcher (exactly one mention, no
        # self-nomination) — shared so the two paths can never drift.
        member_id = recipient_of(mentions, int(author_id))
        if member_id is None:
            continue
        yield int(message_id), float(ts), member_id, int(author_id)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--guild", type=int, required=True, help="guild id")
    ap.add_argument("--channel", type=int, required=True, help="the game's channel id")
    ap.add_argument(
        "--amount", type=int, required=True, help="coins per turn (no default: "
        "this opens a faucet, so it must be stated)"
    )
    args = ap.parse_args()

    if args.amount < 1:
        log.error("--amount must be at least 1.")
        return 1

    with open_db(args.db) as conn:
        settings = load_econ_settings(conn, args.guild)
        if not settings.enabled:
            log.error("Economy is disabled for guild %s — nothing to do.", args.guild)
            return 1
        offset = get_tz_offset_hours(conn, args.guild)

        per_user: Counter[int] = Counter()
        turns: Counter[int] = Counter()
        total = 0

        found = list(_announcements(conn, args.guild, args.channel))
        ids = sorted({uid for _, _, m, a in found for uid in (m, a)})
        names = get_known_user_names_bulk(conn, args.guild, ids)
        name = lambda uid: names.get(uid, str(uid))  # noqa: E731

        for message_id, ts, member_id, announcer_id in found:
            day = local_day_for(ts, offset)
            log.info(
                "  %s  %-24s  named by %s",
                day, name(member_id), name(announcer_id),
            )

            if args.apply:
                # Claim first, same transaction as the credit — the one-time
                # guarantee the live watcher relies on too.
                if not claim_payout_sync(conn, message_id, args.guild, PAYOUT_KIND):
                    continue  # raced/duplicate — already paid

                apply_credit(
                    conn, args.guild, member_id, args.amount, PAYOUT_KIND,
                    meta={"backfill": "pre-watcher", "announced_by": announcer_id},
                    booster=False,
                )
                # The turn's own local day, so a closed board stays closed.
                fire_trigger_quests(
                    conn, settings, args.guild, PAYOUT_KIND, member_id,
                    local_day=day,
                    occurrence=quest_occurrence(message_id),
                    booster=False,
                )

            per_user[member_id] += args.amount
            turns[member_id] += 1
            total += args.amount

        verb = "Credited" if args.apply else "Would credit"
        log.info("—")
        for uid, coins in per_user.most_common():
            log.info(
                "  %s %-24s %2d turn(s) %6d coins",
                verb, name(uid), turns[uid], coins,
            )
        log.info("%s %d coins across %d members", verb, total, len(per_user))
        if not args.apply:
            log.info("Dry run — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
