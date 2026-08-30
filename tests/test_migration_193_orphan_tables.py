"""Migration 193: three tables with no reader anywhere in src/ are dropped.

``give_role_permissions`` and ``music_channel_settings`` both name a member
(``entity_id`` / ``updated_by_user_id``) while being invisible to every surface
an admin or a data subject could reach, so they were personal-data stores
nobody was accounting for. ``dm_request_channels`` stored a channel the DM
request flow never posted to.

The one that needs pinning is ``dm_request_channels``: ``000_init.sql`` still
creates it, so a fresh database builds it and this migration removes it again.
If that ordering ever inverts, a fresh install grows the table back and the
next audit re-finds it.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

DROPPED = ("give_role_permissions", "dm_request_channels", "music_channel_settings")


def _tables(db_path) -> set[str]:
    with open_db(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_the_three_orphans_are_gone_after_the_full_chain(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    assert _tables(db).isdisjoint(DROPPED)


def test_the_live_grant_allow_list_survives(tmp_path):
    """``grant_role_permissions`` is the table the Role Grants panel writes —
    one character away from the legacy name this migration drops."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    assert "grant_role_permissions" in _tables(db)
    assert "grant_roles" in _tables(db)


def test_the_drop_survives_a_table_recreated_after_the_first_pass(tmp_path):
    """A boot re-runs the chain. If an older code path recreated one of these,
    re-applying 193 has to take it away again rather than skip as applied."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.execute(
            "CREATE TABLE dm_request_channels "
            "(guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)"
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '193%'")
    migrations.apply_migrations_sync(db)

    assert "dm_request_channels" not in _tables(db)
