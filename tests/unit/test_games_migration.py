"""Verify 019_games.sql creates all expected tables and seeds LegitLibs data."""

from __future__ import annotations

from bot_modules.core.db_utils import open_db

EXPECTED_TABLES = [
    "games_consent",
    "games_allowed_channels",
    "games_active_games",
    "games_question_bank",
    "games_game_history",
    "games_session_tracker",
    "games_timer_defaults",
    "games_audit_channel",
    "legitlibs_blank_axes",
    "legitlibs_blank_prompts",
    "legitlibs_templates",
    "legitlibs_revisions",
    "legitlibs_reports",
    "legitlibs_channel_config",
    "legitlibs_recent_use",
]


def test_all_games_tables_exist(sync_db_path):
    with open_db(sync_db_path) as conn:
        existing = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    for table in EXPECTED_TABLES:
        assert table in existing, f"Missing table: {table}"


def test_legitlibs_blank_axes_has_all_pos_values(sync_db_path):
    with open_db(sync_db_path) as conn:
        rows = conn.execute(
            "SELECT value FROM legitlibs_blank_axes WHERE axis = 'pos'"
        ).fetchall()
    values = {r["value"] for r in rows}
    assert values == {"noun", "verb", "adjective", "adverb", "exclamation", "number", "wildcard"}


def test_legitlibs_blank_axes_noun_domains_seeded(sync_db_path):
    with open_db(sync_db_path) as conn:
        rows = conn.execute(
            "SELECT value FROM legitlibs_blank_axes WHERE axis = 'domain' AND parent_pos = 'noun'"
        ).fetchall()
    values = {r["value"] for r in rows}
    assert {"place", "person", "body", "kink"} <= values


def test_legitlibs_blank_axes_verb_forms_seeded(sync_db_path):
    with open_db(sync_db_path) as conn:
        rows = conn.execute(
            "SELECT value FROM legitlibs_blank_axes WHERE axis = 'form' AND parent_pos = 'verb'"
        ).fetchall()
    values = {r["value"] for r in rows}
    assert {"ing", "past", "infinitive"} <= values


def test_legitlibs_blank_axes_total_count(sync_db_path):
    with open_db(sync_db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM legitlibs_blank_axes").fetchone()[0]
    # 7 pos + 4 noun domains + 1 verb domain + 3 verb forms + 1 noun form = 16
    assert count == 16


def test_legitlibs_blank_prompts_seeded(sync_db_path):
    with open_db(sync_db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM legitlibs_blank_prompts").fetchone()[0]
    # Counted from the seed INSERT statements in the migration
    assert count == 46


def test_legitlibs_blank_prompts_covers_all_tiers(sync_db_path):
    with open_db(sync_db_path) as conn:
        tiers = {
            r["tier"]
            for r in conn.execute(
                "SELECT DISTINCT tier FROM legitlibs_blank_prompts"
            ).fetchall()
        }
    assert tiers == {1, 2, 3, 4}


def test_games_question_bank_added_by_defaults_to_zero(sync_db_path):
    """Schema smoke: added_by DEFAULT 0 means inserts without it succeed."""
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO games_question_bank (game_type, category, question_text)"
            " VALUES ('wyr', 'sfw', 'test?')"
        )
    with open_db(sync_db_path) as conn:
        row = conn.execute("SELECT * FROM games_question_bank").fetchone()
    assert row["added_by"] == 0


def test_games_question_bank_tags_column_defaults_to_empty_json(sync_db_path):
    """Migration 053 adds a JSON tags column defaulting to '[]'."""
    with open_db(sync_db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(games_question_bank)")}
        assert "tags" in cols
        conn.execute(
            "INSERT INTO games_question_bank (game_type, question_text)"
            " VALUES ('wyr', 'no tags?')"
        )
    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT tags FROM games_question_bank WHERE question_text = 'no tags?'"
        ).fetchone()
    assert row["tags"] == "[]"


def test_games_question_bank_ffa_prompts_seeded_with_tags(sync_db_path):
    """Migration 054 seeds FFA prompts tagged truth/dare (+nsfw)."""
    import json

    with open_db(sync_db_path) as conn:
        rows = conn.execute(
            "SELECT tags FROM games_question_bank WHERE game_type = 'ffa'"
        ).fetchall()
    assert len(rows) > 0
    tag_sets = [set(json.loads(r["tags"])) for r in rows]
    # Every seeded prompt is tagged exactly one of truth/dare.
    assert all(("truth" in ts) ^ ("dare" in ts) for ts in tag_sets)
    # Both labels and an nsfw subset are present.
    assert any("truth" in ts for ts in tag_sets)
    assert any("dare" in ts for ts in tag_sets)
    assert any("nsfw" in ts for ts in tag_sets)


def test_games_allowed_channels_added_by_defaults_to_zero(sync_db_path):
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO games_allowed_channels (channel_id) VALUES (123)"
        )
    with open_db(sync_db_path) as conn:
        row = conn.execute("SELECT * FROM games_allowed_channels").fetchone()
    assert row["added_by"] == 0


def test_legitlibs_templates_autoincrement_id(sync_db_path):
    with open_db(sync_db_path) as conn:
        cur = conn.execute(
            "INSERT INTO legitlibs_templates (title, body, tier)"
            " VALUES ('T', 'B {1}', 1)"
        )
        tid = cur.lastrowid
    assert isinstance(tid, int)
    assert tid > 0
    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT * FROM legitlibs_templates WHERE template_id = ?", (tid,)
        ).fetchone()
    assert row is not None
    assert row["title"] == "T"


# ── migration 124: legitlibs_templates guild scope + single-guild backfill ──

def _seed_template(conn, template_id, guild_id):
    conn.execute(
        "INSERT INTO legitlibs_templates "
        "(template_id, title, body, tier, tags, status, blanks, guild_id) "
        "VALUES (?, 'T', 'a {1} b', 1, '[]', 'published', '[]', ?)",
        (template_id, guild_id),
    )


def _backfill(conn):
    conn.execute(
        "UPDATE legitlibs_templates SET guild_id = "
        "(SELECT guild_id FROM games_game_config LIMIT 1) "
        "WHERE guild_id = 0 "
        "AND (SELECT COUNT(DISTINCT guild_id) FROM games_game_config) = 1"
    )


def test_legitlibs_templates_has_guild_id(sync_db_path):
    with open_db(sync_db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(legitlibs_templates)")}
        assert "guild_id" in cols


def test_single_guild_backfill_assigns_existing_templates(sync_db_path):
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO games_game_config (guild_id, game_type) VALUES (500, 'legitlibs')"
        )
        _seed_template(conn, 1, 0)  # legacy global row
        _backfill(conn)
        assert conn.execute(
            "SELECT guild_id FROM legitlibs_templates WHERE template_id = 1"
        ).fetchone()[0] == 500


def test_multi_guild_backfill_leaves_templates_global(sync_db_path):
    with open_db(sync_db_path) as conn:
        conn.executemany(
            "INSERT INTO games_game_config (guild_id, game_type) VALUES (?, 'legitlibs')",
            [(500,), (600,)],
        )
        _seed_template(conn, 1, 0)
        _backfill(conn)
        assert conn.execute(
            "SELECT guild_id FROM legitlibs_templates WHERE template_id = 1"
        ).fetchone()[0] == 0  # never guessed
