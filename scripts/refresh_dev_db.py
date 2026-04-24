#!/usr/bin/env python3
"""Refresh the dev database from prod using SQLite's backup API (spec §4.2).

Usage:
    python scripts/refresh_dev_db.py [--no-fixtures]

Safety: source is always prod, destination is always dev. Script refuses
to run if destination path does not contain 'dev'.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is importable when run as a script
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from migrations import apply_migrations_sync

_MAX_BACKUPS = 3


def _rotate_backups(dev_path: Path) -> None:
    if not dev_path.exists():
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = dev_path.with_suffix(f".db.bak-{ts}")
    dev_path.rename(backup)
    print(f"Backed up existing dev DB → {backup.name}")

    baks = sorted(dev_path.parent.glob(f"{dev_path.stem}.db.bak-*"))
    for old in baks[:-_MAX_BACKUPS]:
        old.unlink()
        print(f"Deleted old backup: {old.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-fixtures",
        action="store_true",
        help="Skip loading fixtures even if SEED_DEV_FIXTURES=1",
    )
    args = parser.parse_args()

    # Load config — we need both prod and dev paths
    import os

    prod_path_raw = os.environ.get("DB_PATH_PROD", "dungeonkeeper.db")
    dev_path_raw = os.environ.get("DB_PATH_DEV", "dk_dev.db")

    prod_path = _ROOT / prod_path_raw
    dev_path = _ROOT / dev_path_raw

    # Safety: destination must contain 'dev'
    if "dev" not in str(dev_path).lower():
        print(
            f"SAFETY: destination path {dev_path!r} does not contain 'dev'. Refusing to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not prod_path.exists():
        print(f"ERROR: prod DB not found at {prod_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Source (prod): {prod_path}")
    print(f"Destination (dev): {dev_path}")

    _rotate_backups(dev_path)

    print("Copying prod → dev via SQLite backup API...")
    with sqlite3.connect(str(prod_path)) as src, sqlite3.connect(str(dev_path)) as dst:
        src.backup(dst)
    print("Copy complete.")

    print("Applying pending migrations to dev DB...")
    apply_migrations_sync(dev_path)
    print("Migrations applied.")

    if not args.no_fixtures and os.environ.get("SEED_DEV_FIXTURES") == "1":
        print("Loading dev fixtures...")
        from scripts.load_fixtures import load_fixtures  # type: ignore[import]

        load_fixtures(dev_path)
        print("Fixtures loaded.")

    print("Dev DB refresh complete.")


if __name__ == "__main__":
    main()
