#!/usr/bin/env python3
"""Subject access / portability export — GDPR Art 15 and Art 20.

Collects every DB row naming one member into a single JSON file. Read-only:
the database is opened with ``mode=ro`` so this can never touch production
state, and it is safe to run against a live bot.

    python scripts/export_user_data.py --guild 123 --user 456
    python scripts/export_user_data.py --guild 123 --user 456 --out sar.json
    python scripts/export_user_data.py --guild 123 --user 456 --summary

The export is deliberately **wider than the erasure path**: data the server
keeps under Art 17(3) (the economy ledger, sanction history, consent audit,
no-contact orders) is still the subject's personal data and still has to be
disclosed. See docs/gdpr_runbook.md for the full procedure.

**Read the review list before sending anything.** Tables that name a second
member are listed under ``review_required`` — Art 15(4) says an access request
must not adversely affect the rights of others, so those rows need a human
decision about redaction before the file leaves the building.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.services.privacy_service import export_user_data  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "dungeonkeeper.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--guild", type=int, required=True)
    ap.add_argument("--user", type=int, required=True)
    ap.add_argument(
        "--out",
        type=Path,
        help="output file (default: export-<guild>-<user>-<date>.json)",
    )
    ap.add_argument(
        "--summary",
        action="store_true",
        help="print the per-table row counts instead of writing a file",
    )
    args = ap.parse_args()

    if not args.db.exists():
        print(f"No such database: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        data = export_user_data(conn, args.guild, args.user)
    finally:
        conn.close()

    stamp = datetime.now(timezone.utc)
    data["subject"]["exported_at"] = stamp.isoformat()
    data["subject"]["source_db"] = str(args.db)

    total = sum(data["counts"].values())

    if args.summary:
        print(f"Subject {args.user} in guild {args.guild} — {total:,} rows")
        for table, n in sorted(
            data["counts"].items(), key=lambda kv: (-kv[1], kv[0])
        ):
            flag = " [third party]" if table in data["review_required"] else ""
            print(f"  {n:>9,}  {table}{flag}")
    else:
        out = args.out or PROJECT_ROOT / (
            f"export-{args.guild}-{args.user}-{stamp:%Y-%m-%d}.json"
        )
        out.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"Wrote {out} — {len(data['counts'])} tables, {total:,} rows")

    if data["review_required"]:
        print(
            "\nArt 15(4) review needed — these tables name another member:\n  "
            + "\n  ".join(data["review_required"])
        )
    if data["notes"]:
        print("\nNotes:\n  " + "\n  ".join(data["notes"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
