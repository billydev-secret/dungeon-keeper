#!/usr/bin/env python3
"""Archive the QA cards left pending by the per-commit era.

One-off cleanup, safe to re-run. Until branch cards landed, every commit with
a ``Testing:`` section posted its own card — 442 of them in the 30 days to
2026-08-20, against 21 verdicts ever recorded. What that left behind is a
queue of 172 pending cards, 165 of them created on or before 2026-07-21 and
none of them carrying a single verdict: nobody was ever going to work through
it, and it buries the handful of cards that are actually current.

This archives the stale block and edits each card's Discord message so it
reads archived and its Pass/Fail/Blocked buttons are gone. Cards that carry a
verdict are never touched — someone's work is recorded on those, and
``status`` is computed from the verdicts anyway.

    python scripts/archive_stale_qa_cards.py                  # dry run
    python scripts/archive_stale_qa_cards.py --apply

``--cutoff`` moves the date; the default is the end of the stale block.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import post_testing_docs as ptd  # noqa: E402  (path shim above must run first)

DEFAULT_CUTOFF = "2026-07-21"


def valid_cutoff(text: str) -> str:
    """``YYYY-MM-DD`` or an argparse error.

    The comparison below is lexicographic, which makes a plausible typo
    destructive rather than merely wrong: ``2026-7-21`` (no zero pad) sorts
    *above* every real ``2026-08-…`` timestamp at the sixth character, so the
    query would select the entire live queue and --apply would archive it.
    """
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        parsed = None
    # strptime itself is lenient about zero-padding -- it accepts "2026-7-21"
    # happily -- so the round trip, not the parse, is what enforces the format.
    if parsed is None or parsed.strftime("%Y-%m-%d") != text:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a zero-padded YYYY-MM-DD date"
        )
    return text


def stale_pending(conn, cutoff: str) -> list[dict]:
    """Pending, verdict-free cards created on or before ``cutoff``.

    ``created_at`` is a UTC ISO timestamp, so a plain string comparison
    against ``<date>T23:59:59`` covers the whole cutoff day — sound only
    because ``valid_cutoff`` has already rejected an unpadded date.
    """
    conn.row_factory = __import__("sqlite3").Row
    rows = conn.execute(
        """
        SELECT t.id, t.title, t.channel_id, t.message_id, t.created_at,
               t.commit_sha, t.commit_subject, t.body_md
        FROM qa_tests t
        WHERE t.status = 'pending'
          AND t.created_at <= ?
          AND NOT EXISTS (
              SELECT 1 FROM qa_verdicts v WHERE v.test_id = t.id
          )
        ORDER BY t.created_at
        """,
        (f"{cutoff}T23:59:59+00:00",),
    ).fetchall()
    return [dict(row) for row in rows]


def archive_row(conn, test_id: int) -> None:
    conn.execute(
        "UPDATE qa_tests SET status = 'archived', updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), test_id),
    )
    conn.commit()


def retire_message(row: dict, tok: str) -> bool:
    """Re-render the posted card as archived, with no buttons. True if edited."""
    if not row.get("channel_id") or not row.get("message_id"):
        return False
    test = {
        "title": row["title"],
        "body_md": row["body_md"],
        "status": "archived",
        "commit_sha": row["commit_sha"],
        "commit_subject": row["commit_subject"],
    }
    ptd.request(
        "PATCH",
        f"{ptd.API}/channels/{row['channel_id']}/messages/{row['message_id']}",
        tok,
        {"embeds": [ptd.build_card_embed(test, [])], "components": []},
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--cutoff", default=DEFAULT_CUTOFF, type=valid_cutoff, metavar="YYYY-MM-DD"
    )
    ap.add_argument("--apply", action="store_true", help="actually archive")
    args = ap.parse_args()

    conn = ptd.qa_connect()
    if conn is None:
        return 1
    rows = stale_pending(conn, args.cutoff)
    print(f"{len(rows)} pending card(s) created on or before {args.cutoff}")
    if not rows:
        conn.close()
        return 0
    print(f"  oldest {rows[0]['created_at'][:10]}  newest {rows[-1]['created_at'][:10]}")

    if not args.apply:
        for row in rows[:5]:
            print(f"  - {row['created_at'][:10]}  {row['title'][:70]}")
        if len(rows) > 5:
            print(f"  … and {len(rows) - 5} more")
        print("\ndry run — pass --apply to archive these")
        conn.close()
        return 0

    tok = ptd.token()
    edited = 0
    for index, row in enumerate(rows, 1):
        archive_row(conn, row["id"])
        try:
            if retire_message(row, tok):
                edited += 1
        except (Exception, SystemExit) as exc:
            # A card whose message was deleted by hand still archives in the
            # DB; the row is the record, the message is just its rendering.
            print(f"  message {row['message_id']}: {exc}")
        time.sleep(0.4)
        print(f"  [{index}/{len(rows)}]", end="\r", flush=True)
    conn.close()
    print(f"archived {len(rows)} card(s); {edited} message(s) edited        ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
