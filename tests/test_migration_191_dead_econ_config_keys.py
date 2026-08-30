"""Migration 191: three economy config keys nothing reads are cleared out.

Two of them (``econ_leaderboard_channel_id`` / ``_message_id``) were retired on
2026-08-18 when the how-to guide and the leaderboard merged into one panel, and
zeroed on the next boot by a startup one-shot. The 2026-08-29 channel-settings
audit confirmed every guild had restarted past that — two carried an explicit 0
and the third had no row — so the one-shot, the pure planner behind it and the
``EconSettings`` fields all went in the same commit as this migration.

The third (``econ_price_gift_color``) is the older kind of leftover: the
``gift_color`` perk was retired in migration 091 when gifting was widened to
every perk, but its *price* row outlived it in two guilds. A config key with no
reader is a silent no-op, which is only harmless while nobody mistakes it for a
setting.

Deleting the rows rather than zeroing them is the point of the migration, so
that is what these assert — a 0 left behind still reads as a value somebody set.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 1469491362444480666
DEAD = (
    "econ_leaderboard_channel_id",
    "econ_leaderboard_message_id",
    "econ_price_gift_color",
)


def _keys(db_path, guild_id=GUILD) -> set[str]:
    with open_db(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT key FROM config WHERE guild_id = ?", (guild_id,)
            )
        }


def test_the_dead_keys_are_gone_after_the_full_chain(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [(GUILD, key, "50") for key in DEAD],
        )
    # Re-running the chain is what a boot does; 191 is idempotent, so the
    # second pass is what actually clears rows inserted after the first.
    migrations.apply_migrations_sync(db)

    with open_db(db) as conn:
        conn.execute(
            "DELETE FROM schema_version WHERE migration LIKE '191%'"
        )
    migrations.apply_migrations_sync(db)

    assert _keys(db) & set(DEAD) == set()


def test_it_leaves_the_surviving_panel_ids_alone(tmp_path):
    """``guide_*`` is the pair the merged panel actually uses — deleting it
    would orphan every posted economy panel."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        conn.executemany(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            [
                (GUILD, "econ_guide_channel_id", "1526017396094144584"),
                (GUILD, "econ_guide_message_id", "1528528402892722272"),
                (GUILD, "econ_bounty_channel_id", "1532059736038441200"),
                (GUILD, "econ_leaderboard_channel_id", "0"),
            ],
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '191%'")
    migrations.apply_migrations_sync(db)

    survivors = _keys(db)
    assert "econ_guide_channel_id" in survivors
    assert "econ_guide_message_id" in survivors
    assert "econ_bounty_channel_id" in survivors
    assert "econ_leaderboard_channel_id" not in survivors


def test_econ_settings_no_longer_carries_the_retired_fields():
    """The fields went with the rows. Leaving them would keep the dashboard's
    config payload advertising a panel pair that can never be set."""
    import dataclasses

    from bot_modules.services.economy_service import EconSettings

    names = {f.name for f in dataclasses.fields(EconSettings)}
    assert "leaderboard_channel_id" not in names
    assert "leaderboard_message_id" not in names
    assert "guide_channel_id" in names  # the pair that survived the merge
