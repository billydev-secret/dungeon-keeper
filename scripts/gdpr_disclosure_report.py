#!/usr/bin/env python3
"""Readable GDPR disclosure report — the Art 12(1) layer over the Art 15 export.

``scripts/export_user_data.py`` produces the complete answer to a subject access
request and, for an active member, produces it as roughly 280,000 rows of JSON.
That is the portability artefact, not something a person can read. This script
turns the same export into a Markdown document a member can be sent: what
categories of data exist about them, why each is held, how long, how much of it,
and what happens to it if they ask for erasure.

    python scripts/gdpr_disclosure_report.py --guild 123 --user 456
    python scripts/gdpr_disclosure_report.py --guild 123 --user 456 --with-export

Read-only: the database is opened ``mode=ro``, so it is safe against the live
bot. Operator warnings print to the terminal and never to the document.

The output names a real person and is personal data in its own right. It
defaults to ``private/`` — which is gitignored — and must not be committed.
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

from bot_modules.services.disclosure_service import (  # noqa: E402
    build_report,
    render_markdown,
)
from bot_modules.services.privacy_service import export_user_data  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "dungeonkeeper.db"
DEFAULT_OUT_DIR = PROJECT_ROOT / "private"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--guild", type=int, required=True)
    ap.add_argument("--user", type=int, required=True)
    ap.add_argument("--server-name", default="this", help="name used in the prose")
    ap.add_argument("--out", type=Path, help="output .md (default: private/…)")
    ap.add_argument(
        "--with-export",
        action="store_true",
        help="also write the full JSON export beside the report",
    )
    ap.add_argument(
        "--register",
        type=Path,
        default=PROJECT_ROOT / "docs" / "data_register.md",
    )
    args = ap.parse_args()

    if not args.db.exists():
        print(f"No such database: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        export = export_user_data(conn, args.guild, args.user)
        report = build_report(
            conn,
            export,
            args.register.read_text(encoding="utf-8"),
            args.guild,
            args.user,
        )
    finally:
        conn.close()

    stamp = datetime.now(timezone.utc)
    out = args.out or DEFAULT_OUT_DIR / (
        f"disclosure-{args.guild}-{args.user}-{stamp:%Y-%m-%d}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report, args.server_name), encoding="utf-8")
    print(
        f"Wrote {out} — {len(report.categories)} categories, "
        f"{report.total_rows:,} records"
    )

    if args.with_export:
        js = out.with_suffix(".json")
        js.write_text(
            json.dumps(export, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"Wrote {js} — full export, {sum(export['counts'].values()):,} rows")

    # Operator warnings: terminal only. None of this belongs in a member's copy.
    if report.unmapped_tables:
        print(
            "\nNot in the data register — these hold rows for this member and "
            "appear in NO section of the report:\n  "
            + "\n  ".join(report.unmapped_tables),
            file=sys.stderr,
        )
    if report.list_valued_hits:
        print(
            "\nFound inside list-valued columns (the export's documented blind "
            "spot):\n  "
            + "\n  ".join(f"{k}: {v}" for k, v in report.list_valued_hits.items()),
            file=sys.stderr,
        )
    if report.operator_notes:
        print("\nRegister gaps:\n  " + "\n  ".join(report.operator_notes), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
