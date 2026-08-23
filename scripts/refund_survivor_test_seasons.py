"""One-off refund of real coins staked in the cancelled Survivor test seasons.

Seasons 1 ("The Golden League") and 2 ("sim season") were build-test runs,
both closed as ``complete`` with no payout of any survivor kind ever credited
— but their buy-in and gauntlet-fee debits were real ``econ_ledger`` rows
against real wallets (verified 2026-08-22: one 100-coin buy-in on season 1,
ten gauntlet-fee debits totaling 1,850 on season 2). The engine has no cancel
refund path, so this script gives the coins back.

Usage (dry run first — it writes nothing without --apply):

    python -m scripts.refund_survivor_test_seasons
    python -m scripts.refund_survivor_test_seasons --apply

Semantics, mirroring the other one-off scripts here:

* **Idempotent.** Each refund credit carries ``{"refund_of": <ledger id>}``
  in its meta; a debit that already has a matching ``survivor_refund`` row is
  skipped, so re-running is a no-op.
* **Exact amounts, no booster multiplier** — this returns what was taken.
* Season-scoped via ``json_extract`` on meta, never LIKE (the pot_totals
  precedent: '"season_id": 1' is a substring of '"season_id": 12').
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import open_db_immediate  # noqa: E402
from bot_modules.services.economy_service import apply_credit  # noqa: E402
from bot_modules.services.message_store import (  # noqa: E402
    get_known_user_names_bulk,
)

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

REFUND_KIND = "survivor_refund"
DEBIT_KINDS = ("survivor_buyin", "survivor_gauntlet_fee")
TEST_SEASON_IDS = (1, 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write refunds")
    args = parser.parse_args()

    with open_db_immediate(DB_PATH) as conn:
        placeholders = ",".join("?" for _ in TEST_SEASON_IDS)
        debits = conn.execute(
            f"""
            SELECT id, guild_id, user_id, -amount AS amount, kind,
                   json_extract(CASE WHEN json_valid(meta) THEN meta
                                     ELSE '{{}}' END, '$.season_id') AS season_id
            FROM econ_ledger
            WHERE kind IN (?, ?) AND amount < 0
              AND COALESCE(json_extract(CASE WHEN json_valid(meta) THEN meta
                                             ELSE '{{}}' END, '$.season_id'), 0)
                  IN ({placeholders})
            ORDER BY id
            """,
            (*DEBIT_KINDS, *TEST_SEASON_IDS),
        ).fetchall()

        already = {
            row["refund_of"]
            for row in conn.execute(
                """
                SELECT json_extract(CASE WHEN json_valid(meta) THEN meta
                                         ELSE '{}' END, '$.refund_of')
                       AS refund_of
                FROM econ_ledger WHERE kind = ?
                """,
                (REFUND_KIND,),
            )
            if row["refund_of"] is not None
        }

        names: dict[int, str] = {}
        if debits:
            names = get_known_user_names_bulk(
                conn,
                int(debits[0]["guild_id"]),
                [int(d["user_id"]) for d in debits],
            )

        refunded = skipped = total = 0
        for d in debits:
            label = names.get(int(d["user_id"])) or d["user_id"]
            if d["id"] in already:
                skipped += 1
                print(f"skip   {label}: {d['amount']} ({d['kind']} "
                      f"row {d['id']}) — already refunded")
                continue
            print(f"refund {label}: {d['amount']} ({d['kind']}, "
                  f"season {d['season_id']}, row {d['id']})")
            if args.apply:
                apply_credit(
                    conn,
                    int(d["guild_id"]),
                    int(d["user_id"]),
                    int(d["amount"]),
                    REFUND_KIND,
                    meta={
                        "season_id": int(d["season_id"]),
                        "refund_of": int(d["id"]),
                        "reason": "test season cancelled",
                    },
                )
            refunded += 1
            total += int(d["amount"])

        verb = "refunded" if args.apply else "would refund"
        print(f"\n{verb} {total} coins across {refunded} debit(s); "
              f"{skipped} already done."
              + ("" if args.apply else "  (dry run — use --apply)"))


if __name__ == "__main__":
    main()
