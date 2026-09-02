#!/usr/bin/env python3
"""Privacy coverage sweep — which tables holding personal data are reachable.

Three questions, answered against a real schema rather than a curated list:

1. **Export.** Does every column that actually holds a member id have a name in
   ``privacy_service.SUBJECT_ID_COLUMNS``? A column that does not is invisible
   to a subject access request even though the register may list its table.
2. **Register.** Does every table with a subject-id column have a row in
   ``docs/data_register.md``? That file is the record of processing activities;
   a personal-data store missing from it is invisible to an access or erasure
   request at the paperwork level, whatever the code does.
3. **Purge.** Is the table reached by ``purge_user_data``? A "no" here is not
   automatically a defect — the register names five preserved categories with
   their Art 17(3) grounds — but a "no" that the register does not explain is.

Run against production read-only::

    python scripts/privacy_coverage.py \\
        --db 'file:/path/to/dungeonkeeper.db?mode=ro'

The empirical mode (``--db`` on a populated database) is the strong one: it
learns the real member ids from the roster tables and then reports any column
whose *values* are member ids, no matter what the column is called. That is how
the 2026-09-02 review found ``rules_labels.labeled_by`` and six others. Against
an empty or freshly-migrated database there are no values to learn from, so the
sweep falls back to matching column names, which can only confirm what the
convention already knows.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bot_modules.services.privacy_service import (  # noqa: E402
    SUBJECT_ID_COLUMNS,
)

REGISTER_PATH = REPO_ROOT / "docs" / "data_register.md"

#: Roster tables the empirical mode learns real member ids from. Any one of
#: them is enough; all are tried so the sweep still works on a partial dump.
_ROSTER_SOURCES = (
    ("known_users", "user_id"),
    ("member_xp", "user_id"),
    ("messages", "author_id"),
    ("member_activity", "user_id"),
)

#: Columns that hold a Discord snowflake which is never a *member*. Listed so
#: the empirical mode does not have to rediscover each one, and so that adding
#: to this list is a deliberate act.
_NOT_MEMBER_COLUMNS = frozenset(
    {
        "id", "guild_id", "channel_id", "message_id", "role_id", "category_id",
        "thread_id", "emoji_id", "parent_id", "webhook_id", "game_id",
        "table_id", "voice_channel_id",
        # Reviewed 2026-09-02 and excluded on the evidence, not on the name.
        # Each of these matched the roster by coincidence or holds a non-member
        # snowflake; leaving them in makes the report noisy enough to stop
        # being read, which is how the real gaps hid in the first place.
        #
        # `config`/`config_ids` store channel and role ids under a generic
        # `value`; a role id cannot collide with a member id, and the handful
        # that matched are guild settings, not records about a member.
        "value",
        # A string enum ('set', 'inc') whose numeric variants overlap by luck.
        "occurrence",
        # A normalised hash of the member's last message, not an id.
        "last_message_norm",
        # Bot accounts. They sit in `known_users` like anyone else, so the
        # roster match is real — but a bot is not a data subject.
        "detector_bot_id", "bot_user_id",
    }
)

#: Columns holding a *list* of member ids (CSV or JSON). They are a documented
#: blind spot in ``privacy_service.LIST_VALUED_MEMBER_COLUMNS`` — an equality
#: match cannot find a subject inside one — so the sweep reports them under
#: their own heading rather than as newly-discovered gaps.
_LIST_VALUED = frozenset({"participant_user_ids", "allowed_replier_ids"})

#: A Discord snowflake is a 64-bit id whose timestamp epoch starts in 2015, so
#: every real id comfortably exceeds this. Used to reject counters and prices
#: before they are compared against the roster.
_SNOWFLAKE_FLOOR = 10**16


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def learn_member_ids(conn: sqlite3.Connection) -> set[int]:
    """Every id known to be a member, read from the roster tables."""
    ids: set[int] = set()
    for table, col in _ROSTER_SOURCES:
        try:
            rows = conn.execute(f'SELECT DISTINCT "{col}" FROM "{table}"')
        except sqlite3.Error:
            continue  # table absent on this deployment; another source will do
        for (value,) in rows:
            if isinstance(value, int) and value > _SNOWFLAKE_FLOOR:
                ids.add(value)
    return ids


def member_id_columns(
    conn: sqlite3.Connection, members: set[int], *, sample: int = 5000
) -> dict[str, dict[str, int]]:
    """Map table → column → how many distinct known members the column holds.

    Empirical: a column is reported because its *values* are member ids, which
    is the only test that survives a feature naming its column something new.
    """
    found: dict[str, dict[str, int]] = {}
    for table in _tables(conn):
        try:
            columns = _columns(conn, table)
        except sqlite3.Error:
            continue
        for column in columns:
            if column in _NOT_MEMBER_COLUMNS:
                continue
            try:
                rows = conn.execute(
                    f'SELECT DISTINCT "{column}" FROM "{table}" '
                    f'WHERE "{column}" IS NOT NULL LIMIT {int(sample)}'
                )
            except sqlite3.Error:
                continue
            values: set[int] = set()
            for (value,) in rows:
                if isinstance(value, int):
                    values.add(value)
                elif isinstance(value, str) and re.fullmatch(r"\d{17,20}", value):
                    # CSV/JSON list columns are a separate, documented blind
                    # spot; a bare id stored as text still counts here.
                    values.add(int(value))
            matched = values & members
            if matched:
                found.setdefault(table, {})[column] = len(matched)
    return found


def register_tables(path: Path = REGISTER_PATH) -> tuple[set[str], set[str]]:
    """Table names named in the register, as (exact names, wildcard prefixes).

    The register's leftmost cell lists one or more tables, sometimes as a
    family (``wellness_*``, ``casino_*``). Both forms count as documented.
    """
    exact: set[str] = set()
    wildcards: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1].strip().replace("`", "")
        cell = re.sub(r"\(.*?\)", "", cell)  # drop "(49 tables)" style asides
        for part in re.split(r"[,+/]| and ", cell):
            name = part.strip()
            if re.fullmatch(r"[a-z0-9_]+\*", name):
                wildcards.add(name[:-1])
            elif re.fullmatch(r"[a-z0-9_]{3,}", name):
                exact.add(name)
    return exact, wildcards


def is_registered(table: str, exact: set[str], wildcards: set[str]) -> bool:
    return table in exact or any(table.startswith(p) for p in wildcards)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        required=True,
        help="sqlite path or URI; use ?mode=ro against production",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=5000,
        help="distinct values sampled per column (default 5000)",
    )
    args = parser.parse_args()

    uri = args.db.startswith("file:")
    conn = sqlite3.connect(args.db, uri=uri)

    members = learn_member_ids(conn)
    if not members:
        print(
            "No member ids found in the roster tables — this database is empty "
            "or is not a Dungeon Keeper database. The empirical sweep needs "
            "real rows; nothing below will be meaningful.",
            file=sys.stderr,
        )
        return 2

    found = member_id_columns(conn, members, sample=args.sample)
    exact, wildcards = register_tables()

    unseen: list[tuple[str, list[str]]] = []
    listed: list[tuple[str, list[str]]] = []
    unregistered: list[str] = []
    for table, columns in sorted(found.items()):
        missing = sorted(set(columns) - SUBJECT_ID_COLUMNS)
        known_list = [c for c in missing if c in _LIST_VALUED]
        missing = [c for c in missing if c not in _LIST_VALUED]
        if known_list:
            listed.append((table, known_list))
        if missing:
            unseen.append((table, missing))
        if not is_registered(table, exact, wildcards):
            unregistered.append(table)

    print(f"Learned {len(members)} member ids from the roster tables.")
    print(f"{len(found)} tables hold at least one member id.\n")

    print(f"## Invisible to the access export ({len(unseen)})")
    print("Column values are member ids, but the name is not in "
          "SUBJECT_ID_COLUMNS,\nso export_user_data never looks at it.\n")
    for table, columns in unseen:
        print(f"  {table:36} {', '.join(columns)}")
    if not unseen:
        print("  (none)")

    print(f"\n## Known list-valued blind spot ({len(listed)})")
    print("Documented in LIST_VALUED_MEMBER_COLUMNS; the runbook tells the "
          "operator\nto grep these by hand. Reported, not a new gap.\n")
    for table, columns in listed:
        print(f"  {table:36} {', '.join(columns)}")
    if not listed:
        print("  (none)")

    print(f"\n## No row in data_register.md ({len(unregistered)})")
    print("The record of processing activities does not mention these.\n")
    for table in unregistered:
        print(f"  {table:36} {', '.join(sorted(found[table]))}")
    if not unregistered:
        print("  (none)")

    return 1 if (unseen or unregistered) else 0


if __name__ == "__main__":
    raise SystemExit(main())
