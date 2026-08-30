"""Migration 195: config rows nothing reads any more are deleted, not blanked.

Same operation migration 191 did for three economy keys, for the wider set the
2026-08-30 configuration IA audit found. A key left at 0 or "" still reads as a
value somebody chose, which is the whole failure mode: an admin (or the config
advisor) finds it, changes it, and nothing happens.

The dangerous half is the legacy grant block. Those keys share a prefix with
Image Guard's live settings — ``nsfw_classifier_threshold``,
``nsfw_sfw_prevention_mode`` and friends all begin ``nsfw_`` — so a
prefix-matched delete would take out a working NSFW gate. The keys are
enumerated and scoped to guild 0 for exactly that reason, and that is what the
second test pins.
"""

from __future__ import annotations

import migrations
from bot_modules.core.db_utils import open_db

GUILD = 1469491362444480666

DEAD_ANY_GUILD = (
    "ticket_panel_channel_id",
    "ticket_panel_message_id",
    "ai_mod_model",
    "ai_wellness_model",
    "ai_model_clapback",
    "econ_price_text_room",
    "econ_price_voice_room",
    "econ_quest_board_monthly",
    "greeting_watch_notify_user_id",
)

LEGACY_GRANT_KEYS = tuple(
    f"{grant}_{suffix}"
    for grant in ("denizen", "nsfw", "veteran")
    for suffix in ("role_id", "grant_message", "announce_channel_id", "log_channel_id")
)

# Live Image Guard / NSFW-gate settings that share the legacy block's prefix.
LIVE_NSFW_KEYS = (
    "nsfw_classifier_threshold",
    "nsfw_classifier_sfw_threshold",
    "nsfw_observe_age_gated",
    "nsfw_sfw_prevention_mode",
    "nsfw_sfw_prevention_log_channel_id",
)


def _seed(db_path, rows) -> None:
    with open_db(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            rows,
        )
        conn.execute("DELETE FROM schema_version WHERE migration LIKE '195%'")


def _keys(db_path, guild_id) -> set[str]:
    with open_db(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT key FROM config WHERE guild_id = ?", (guild_id,)
            )
        }


def test_the_dead_keys_are_deleted_not_blanked(tmp_path):
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(db, [(GUILD, key, "123") for key in DEAD_ANY_GUILD])
    migrations.apply_migrations_sync(db)

    assert _keys(db, GUILD) & set(DEAD_ANY_GUILD) == set()


def test_it_does_not_take_the_live_nsfw_gate_with_the_legacy_grant_block(tmp_path):
    """The Image Guard keys are the reason the delete enumerates keys instead
    of matching ``nsfw_%``. Losing them would silently un-gate NSFW media."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [(0, key, "1") for key in LEGACY_GRANT_KEYS]
        + [(GUILD, key, "0.5") for key in LIVE_NSFW_KEYS],
    )
    migrations.apply_migrations_sync(db)

    assert _keys(db, 0) & set(LEGACY_GRANT_KEYS) == set()
    assert set(LIVE_NSFW_KEYS) <= _keys(db, GUILD)


def test_the_legacy_grant_block_is_scoped_to_guild_zero(tmp_path):
    """Finding 51 is about the guild-0 rows the one-shot grant migration reads
    through the legacy fallback. A guild's own row is a different question and
    is deliberately left alone."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [(0, "denizen_role_id", "1"), (GUILD, "denizen_role_id", "2")],
    )
    migrations.apply_migrations_sync(db)

    assert "denizen_role_id" not in _keys(db, 0)
    assert "denizen_role_id" in _keys(db, GUILD)


def test_the_multi_subscriber_greeting_watch_key_survives(tmp_path):
    """Only the pre-multi single key goes. Deleting the CSV would unsubscribe
    every greeter on the next boot."""
    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    _seed(
        db,
        [
            (GUILD, "greeting_watch_notify_user_id", "1384378931981058068"),
            (GUILD, "greeting_watch_notify_user_ids", "1384378931981058068"),
            (GUILD, "greeting_watch_enabled", "true"),
        ],
    )
    migrations.apply_migrations_sync(db)

    survivors = _keys(db, GUILD)
    assert "greeting_watch_notify_user_id" not in survivors
    assert "greeting_watch_notify_user_ids" in survivors
    assert "greeting_watch_enabled" in survivors
