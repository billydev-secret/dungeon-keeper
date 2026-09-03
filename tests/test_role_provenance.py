"""``bot_managed_roles`` — the record of what the provisioner actually did.

Migration 203. The table exists because every state the dashboard showed about
a bot-managed role used to be an inference, and two live defects came out of
that guesswork. These tests pin the three things the rest of the feature leans
on: one row per dial, the origin round-trips, and a repoint replaces rather
than accumulates.
"""

from __future__ import annotations

from bot_modules.core.db_utils import open_db
from bot_modules.services.role_provenance import (
    forget_role_provenance,
    read_role_provenance,
    record_role_provenance,
)
from tests.db_template import migrated_db


def _conn(tmp_path, name="prov.db"):
    path = tmp_path / name
    migrated_db(path)
    return open_db(path)


def test_records_and_reads_back_a_creation(tmp_path):
    with _conn(tmp_path) as conn:
        record_role_provenance(conn, 7, "welcome_ping_role_id", 555, "created")
        conn.commit()
        rows = read_role_provenance(conn, 7)
    assert rows["welcome_ping_role_id"].origin == "created"
    assert rows["welcome_ping_role_id"].role_id == 555
    assert rows["welcome_ping_role_id"].guild_id == 7


def test_adoption_is_a_different_fact_from_creation(tmp_path):
    """The whole point of the table: "I made this" and "this was already
    yours" are the two answers, and only the second makes deleting the role
    obviously wrong."""
    with _conn(tmp_path) as conn:
        record_role_provenance(conn, 7, "jailed_role_id", 42, "adopted")
        conn.commit()
        rows = read_role_provenance(conn, 7)
    assert rows["jailed_role_id"].origin == "adopted"


def test_repointing_a_dial_replaces_the_row(tmp_path):
    """One row per dial, describing where it points *now*. History here would
    make this a second audit log, and ``write_audit`` already is one."""
    with _conn(tmp_path) as conn:
        record_role_provenance(conn, 7, "risky_ping_role_id", 1, "created")
        record_role_provenance(conn, 7, "risky_ping_role_id", 2, "adopted")
        conn.commit()
        rows = read_role_provenance(conn, 7)
        count = conn.execute(
            "SELECT COUNT(*) c FROM bot_managed_roles WHERE guild_id = 7"
        ).fetchone()["c"]
    assert count == 1
    assert rows["risky_ping_role_id"].role_id == 2
    assert rows["risky_ping_role_id"].origin == "adopted"


def test_rows_are_scoped_to_one_guild(tmp_path):
    """Every failure this table fixes was a guild reading another guild's
    answer, so cross-guild leakage here would be a joke at its own expense."""
    with _conn(tmp_path) as conn:
        record_role_provenance(conn, 7, "welcome_ping_role_id", 1, "created")
        record_role_provenance(conn, 8, "welcome_ping_role_id", 2, "created")
        conn.commit()
        assert read_role_provenance(conn, 7)["welcome_ping_role_id"].role_id == 1
        assert read_role_provenance(conn, 8)["welcome_ping_role_id"].role_id == 2


def test_forgetting_a_dial_leaves_other_dials_alone(tmp_path):
    """"Stop managing" forgets one dial. It must not read as "the bot has
    never made anything here"."""
    with _conn(tmp_path) as conn:
        record_role_provenance(conn, 7, "welcome_ping_role_id", 1, "created")
        record_role_provenance(conn, 7, "risky_ping_role_id", 2, "created")
        forget_role_provenance(conn, 7, "welcome_ping_role_id")
        conn.commit()
        rows = read_role_provenance(conn, 7)
    assert "welcome_ping_role_id" not in rows
    assert "risky_ping_role_id" in rows


def test_a_zero_role_id_is_not_recorded(tmp_path):
    """A dial pointing at nothing is the absence of a fact, not a fact."""
    with _conn(tmp_path) as conn:
        record_role_provenance(conn, 7, "welcome_ping_role_id", 0, "created")
        conn.commit()
        assert read_role_provenance(conn, 7) == {}


def test_the_table_names_no_member(tmp_path):
    """Why there is no ``docs/data_register.md`` row.

    Guild, dial, role, origin, timestamp. The acting admin is deliberately not
    stored — ``write_audit`` already records who changed a dial, and copying a
    member id here would turn server configuration into personal data with an
    erasure obligation, for no information anybody lacks.
    """
    with _conn(tmp_path) as conn:
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(bot_managed_roles)").fetchall()
        }
    assert cols == {"guild_id", "role_key", "role_id", "origin", "recorded_at"}
