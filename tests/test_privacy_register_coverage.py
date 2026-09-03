"""The register must name every table that holds personal data.

``docs/data_register.md`` is the record of processing activities: for each store
of personal data it records what the data is, how long it is kept, and whether
an erasure clears it or preserves it on a named Art 17(3) ground. A table that
holds a member id and is absent from that file is invisible to an access or
erasure request at the paperwork level, whatever the code happens to do — which
is how ``games_consent`` sat in production for two months with no retention
decision, no purge decision and no lawful basis recorded anywhere.

CLAUDE.md makes a register row part of the per-commit contract for a new
per-user table. This test is what makes that contract enforceable rather than
remembered: it walks a fully-migrated schema and fails when a table with a
subject-id column has no row.

**What it cannot see**, and why the production sweep still matters:

* A table whose member column is named something not yet in
  ``SUBJECT_ID_COLUMNS`` — the same blind spot the export has. Only
  ``scripts/privacy_coverage.py``, reading real values from a real database,
  finds those. It is how ``rules_labels.labeled_by`` and ten others were found.
* A table created by application code rather than by a migration, which does
  not exist in a migrated schema at all (``foolsday_exclusions``).

So this test is the floor, not the ceiling. Run the sweep against production
when the question is "what are we actually holding".
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from bot_modules.services.privacy_service import SUBJECT_ID_COLUMNS
from tests.db_template import migrated_db

REPO_ROOT = Path(__file__).resolve().parent.parent


def _coverage_module():
    """Load the sweep script so the register parser has one implementation.

    The script is not importable as a package (it lives in ``scripts/``), and
    duplicating its markdown parsing here is exactly how the two would drift.
    """
    path = REPO_ROOT / "scripts" / "privacy_coverage.py"
    spec = importlib.util.spec_from_file_location("privacy_coverage", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_table_with_a_subject_column_is_in_the_register(tmp_path):
    coverage = _coverage_module()
    conn = sqlite3.connect(migrated_db(tmp_path / "register.db"))
    exact, wildcards = coverage.register_tables()

    missing: list[str] = []
    for table in coverage._tables(conn):
        columns = set(coverage._columns(conn, table)) & SUBJECT_ID_COLUMNS
        if columns and not coverage.is_registered(table, exact, wildcards):
            missing.append(f"{table} ({', '.join(sorted(columns))})")

    assert not missing, (
        "These tables hold a member id but have no row in "
        "docs/data_register.md.\nAdd one in the same commit, with an explicit "
        "decision: does purge_user_data clear it, or is it preserved — and if "
        "preserved, on what Art 17(3) ground?\n  " + "\n  ".join(sorted(missing))
    )


def test_register_parser_reads_the_real_register():
    """Guard the parser itself: a silent parse failure would pass everything."""
    coverage = _coverage_module()
    exact, wildcards = coverage.register_tables()

    # Sampled from rows that have been in the register since 2026-08 and from
    # the 2026-09-02 backfill, so a format change that stops the leftmost cell
    # being read is caught rather than turning the gate above into a no-op.
    for table in ("econ_ledger", "anon_audit_log", "starboard_posts",
                  "duel_nicks", "qa_verdicts"):
        assert coverage.is_registered(table, exact, wildcards), table
    assert coverage.is_registered("wellness_streaks", exact, wildcards)
    assert not coverage.is_registered("definitely_not_a_table", exact, wildcards)
