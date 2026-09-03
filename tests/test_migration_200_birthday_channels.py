"""Migration 200: birthdays announce to any number of channels.

The fixed main + second channel (``birthday_channel_id``/``_message``/``_pin``
and their ``_2`` twins) become rows in ``birthday_channels`` — the part most
likely to be got wrong is losing an admin's existing channels on upgrade, so
every test here seeds the *old* shape and checks the *new* one comes out
whole.

Mirrors the re-arm idiom test_migration_195_dead_config_rows.py uses: migrate
a fresh db first (so every later migration's tables exist), seed old-style
config rows, delete migration 200's own schema_version row so it runs again,
then re-migrate and inspect the result.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 1469491362444480666
DEFAULT_MESSAGE = "Happy birthday, {mention}! 🎂\n{request}"


def _seed(db_path, rows) -> None:
    with open_db(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            rows,
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '200%'")


def _channels(db_path, guild_id) -> list[tuple[int, str, int]]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT channel_id, message, pin FROM birthday_channels "
            "WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ).fetchall()
        return [(r["channel_id"], r["message"], r["pin"]) for r in rows]


def _config_keys(db_path, guild_id) -> set[str]:
    with open_db(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT key FROM config WHERE guild_id = ?", (guild_id,)
            )
        }


def test_main_and_second_channel_both_survive_with_their_own_message_and_pin(
    tmp_path,
):
    """The scenario the task calls out by name: an admin who has a main and a
    second channel set today must end up with both in the new list."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [
            (GUILD, "birthday_channel_id", "5555"),
            (GUILD, "birthday_message", "Main: happy birthday {mention}!"),
            (GUILD, "birthday_pin", "1"),
            (GUILD, "birthday_channel_id_2", "6666"),
            (GUILD, "birthday_message_2", "Second: cheers {name}!"),
            (GUILD, "birthday_pin_2", "0"),
        ],
    )
    migrations.apply_migrations_sync(db)

    assert _channels(db, GUILD) == [
        (5555, "Main: happy birthday {mention}!", 1),
        (6666, "Second: cheers {name}!", 0),
    ]


def test_main_channel_only_gets_the_default_message_and_no_pin(tmp_path):
    """No birthday_message/_pin rows stored — the old read path fell back to
    the code default and pin=off; the migrated row must match, not go blank."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(db, [(GUILD, "birthday_channel_id", "7777")])
    migrations.apply_migrations_sync(db)

    assert _channels(db, GUILD) == [(7777, DEFAULT_MESSAGE, 0)]


def test_channel_id_zero_means_disabled_and_gets_no_row(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(db, [(GUILD, "birthday_channel_id", "0")])
    migrations.apply_migrations_sync(db)

    assert _channels(db, GUILD) == []


def test_a_guild_that_never_configured_birthdays_gets_no_rows(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    # No _seed at all for this guild — migration 200 runs, but there is
    # nothing of this guild's to migrate.
    migrations.apply_migrations_sync(db)

    assert _channels(db, GUILD) == []


def test_main_and_second_pointed_at_the_same_channel_keep_the_main_row(tmp_path):
    """The old announcement loop deduped by channel id and skipped a repeat —
    "don't announce twice in one channel". The unique(guild_id, channel_id)
    constraint reproduces that: the second insert is ignored, so the second
    channel's message is dropped rather than silently overwriting the main
    one's — same outcome the old loop had (main wins, second never sends)."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [
            (GUILD, "birthday_channel_id", "8888"),
            (GUILD, "birthday_message", "Main message"),
            (GUILD, "birthday_channel_id_2", "8888"),
            (GUILD, "birthday_message_2", "Second message — never used"),
        ],
    )
    migrations.apply_migrations_sync(db)

    assert _channels(db, GUILD) == [(8888, "Main message", 0)]


def test_old_config_keys_are_deleted_after_migrating(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [
            (GUILD, "birthday_channel_id", "5555"),
            (GUILD, "birthday_message", "Main"),
            (GUILD, "birthday_pin", "1"),
            (GUILD, "birthday_channel_id_2", "6666"),
            (GUILD, "birthday_message_2", "Second"),
            (GUILD, "birthday_pin_2", "0"),
        ],
    )
    migrations.apply_migrations_sync(db)

    survivors = _config_keys(db, GUILD)
    assert survivors.isdisjoint({
        "birthday_channel_id", "birthday_message", "birthday_pin",
        "birthday_channel_id_2", "birthday_message_2", "birthday_pin_2",
    })


def test_announce_hour_is_untouched_by_the_migration(tmp_path):
    """The hour stays a single guild-wide dial, not per-channel — it must
    survive the same migration that deletes the channel-shaped keys."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [
            (GUILD, "birthday_channel_id", "5555"),
            (GUILD, "birthday_announce_hour", "17"),
        ],
    )
    migrations.apply_migrations_sync(db)

    assert "birthday_announce_hour" in _config_keys(db, GUILD)
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE guild_id = ? AND key = 'birthday_announce_hour'",
            (GUILD,),
        ).fetchone()
    assert row["value"] == "17"


def test_two_guilds_migrate_independently(tmp_path):
    other_guild = 999
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [
            (GUILD, "birthday_channel_id", "5555"),
            (other_guild, "birthday_channel_id", "1111"),
        ],
    )
    migrations.apply_migrations_sync(db)

    assert _channels(db, GUILD) == [(5555, DEFAULT_MESSAGE, 0)]
    assert _channels(db, other_guild) == [(1111, DEFAULT_MESSAGE, 0)]
