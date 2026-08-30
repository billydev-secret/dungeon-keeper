"""The "notify moderators when a ticket is opened" toggle, per guild.

The dashboard writes ``ticket_notify_on_create`` against whichever guild the
admin has switched to, so the enforcing read has to name that guild. A read
that leaves it out sees only the legacy ``guild_id = 0`` row, and on a server
that has none the toggle silently does nothing.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.moderation import ticket_notify_on_create_enabled

GUILD = 4242
OTHER_GUILD = 777


def test_notify_defaults_to_on_when_nothing_is_stored(sync_db_path):
    with open_db(sync_db_path) as conn:
        assert ticket_notify_on_create_enabled(conn, GUILD) is True


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("0", False), ("1", True), ("", True)],
)
def test_notify_reads_the_guilds_own_row(sync_db_path, stored, expected):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "ticket_notify_on_create", stored, GUILD)
    with open_db(sync_db_path) as conn:
        assert ticket_notify_on_create_enabled(conn, GUILD) is expected


def test_turning_it_off_on_one_guild_leaves_the_others_alone(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "ticket_notify_on_create", "0", GUILD)
    with open_db(sync_db_path) as conn:
        assert ticket_notify_on_create_enabled(conn, GUILD) is False
        assert ticket_notify_on_create_enabled(conn, OTHER_GUILD) is True
