"""Migration 144: seed configured mod roles into every grant's allow-list.

``can_use_grant_role`` used to short-circuit on ``is_mod``, so a guild could
rely on that bypass and leave ``grant_role_permissions`` empty. Switching the
gate to ``is_admin`` (so the allow-list can actually *restrict*) would silently
revoke every moderator's access to those grants. This migration copies the
configured mod roles in first, making the flip behavior-neutral on day one.

Prod at the time of writing: 6 grants, of which 5 had no permission rows at all
and one (``denizen``) listed only the greeter role.
"""

from __future__ import annotations

import sqlite3

import migrations

GUILD = 1469491362444480666
OTHER_GUILD = 1502099268188639293
MOD_ROLE = 1469783611229339912
GREETER_ROLE = 1470278713504694302
OTHER_MOD_ROLE = 1502358216267403475


def _apply_before_144(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations, "_migration_files",
        lambda: [f for f in real if f.name < "144"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _seed_prod_shape(db_path) -> None:
    """The prod state this migration was written against."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, 'mod_role_ids', ?)",
        (GUILD, str(MOD_ROLE)),
    )
    # The legacy guild_id=0 duplicate that must NOT leak into other guilds.
    conn.execute(
        "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (0, 'mod_role_ids', ?)",
        (str(MOD_ROLE),),
    )
    for name in ("denizen", "goldengirl", "kink", "nsfw", "theboys", "veteran"):
        conn.execute(
            "INSERT OR IGNORE INTO grant_roles (guild_id, grant_name, label) VALUES (?, ?, ?)",
            (GUILD, name, name.title()),
        )
    # denizen already has a hand-set keeper; it must survive.
    conn.execute(
        "INSERT OR IGNORE INTO grant_role_permissions "
        "(guild_id, grant_name, entity_type, entity_id) VALUES (?, 'denizen', 'role', ?)",
        (GUILD, GREETER_ROLE),
    )
    conn.commit()
    conn.close()


def _perms(db_path, guild_id: int = GUILD) -> set[tuple[str, str, int]]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT grant_name, entity_type, entity_id FROM grant_role_permissions "
        "WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    conn.close()
    return {tuple(r) for r in rows}


def test_every_grant_gains_the_mod_role(tmp_path, monkeypatch):
    db = tmp_path / "seed.db"
    _apply_before_144(db, monkeypatch)
    _seed_prod_shape(db)

    # Before: 5 grants are empty and rely purely on the old is_mod bypass.
    assert _perms(db) == {("denizen", "role", GREETER_ROLE)}

    migrations.apply_migrations_sync(db)

    after = _perms(db)
    for name in ("denizen", "goldengirl", "kink", "nsfw", "theboys", "veteran"):
        assert (name, "role", MOD_ROLE) in after, f"{name} lost its moderators"


def test_a_hand_set_keeper_survives(tmp_path, monkeypatch):
    """denizen's greeter role must be added to, not replaced."""
    db = tmp_path / "keeper.db"
    _apply_before_144(db, monkeypatch)
    _seed_prod_shape(db)
    migrations.apply_migrations_sync(db)

    after = _perms(db)
    assert ("denizen", "role", GREETER_ROLE) in after
    assert ("denizen", "role", MOD_ROLE) in after


def test_seeding_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "idem.db"
    _apply_before_144(db, monkeypatch)
    _seed_prod_shape(db)
    migrations.apply_migrations_sync(db)
    once = _perms(db)

    # Re-running the statement must not duplicate or multiply rows.
    sql = (migrations._MIGRATIONS_DIR / "144_grant_permissions_seed_mods.sql").read_text(
        encoding="utf-8"
    )
    conn = sqlite3.connect(db)
    conn.executescript(sql)
    conn.commit()
    conn.close()

    assert _perms(db) == once


def test_the_legacy_guild_zero_row_does_not_leak_across_guilds(tmp_path, monkeypatch):
    """guild_id=0 duplicates the home guild's mods — it must not seed others."""
    db = tmp_path / "leak.db"
    _apply_before_144(db, monkeypatch)
    _seed_prod_shape(db)

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, 'mod_role_ids', ?)",
        (OTHER_GUILD, str(OTHER_MOD_ROLE)),
    )
    conn.execute(
        "INSERT OR IGNORE INTO grant_roles (guild_id, grant_name, label) VALUES (?, 'nsfw', 'NSFW')",
        (OTHER_GUILD,),
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)

    other = _perms(db, OTHER_GUILD)
    assert other == {("nsfw", "role", OTHER_MOD_ROLE)}
    assert MOD_ROLE not in {eid for _, _, eid in other}


def test_a_guild_with_no_mod_roles_seeds_nothing(tmp_path, monkeypatch):
    """No mod_role_ids means no rows — not a row of 0, which would match nobody."""
    db = tmp_path / "nomods.db"
    _apply_before_144(db, monkeypatch)

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, 'mod_role_ids', '')",
        (GUILD,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO grant_roles (guild_id, grant_name, label) VALUES (?, 'nsfw', 'NSFW')",
        (GUILD,),
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)
    assert _perms(db) == set()


def test_a_multi_role_csv_seeds_every_mod_role(tmp_path, monkeypatch):
    """mod_role_ids is a CSV — the recursive split must yield all of them."""
    db = tmp_path / "csv.db"
    _apply_before_144(db, monkeypatch)

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, 'mod_role_ids', ?)",
        (GUILD, "111, 222,333"),  # stray space is realistic hand-entry
    )
    conn.execute(
        "INSERT OR IGNORE INTO grant_roles (guild_id, grant_name, label) VALUES (?, 'nsfw', 'NSFW')",
        (GUILD,),
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)
    assert _perms(db) == {
        ("nsfw", "role", 111),
        ("nsfw", "role", 222),
        ("nsfw", "role", 333),
    }
