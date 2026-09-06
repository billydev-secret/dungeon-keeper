"""scripts/games_review_p0.py — the P0 prod-state writes from the games deep review."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import games_review_p0 as p0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Would you rather sneeze glitter or burp bubbles?", ("sneeze glitter", "burp bubbles"), id="plain-or"),
        pytest.param(
            "Would you rather be kissed against the wall, or pulled in by the waist?",
            ("be kissed against the wall", "pulled in by the waist"),
            id="comma-or",
        ),
        pytest.param(
            "Would you rather have legs for fingers, or fingers for legs?",
            ("have legs for fingers", "fingers for legs"),
            id="or-inside-a-half-splits-on-the-last",
        ),
        pytest.param("Would you rather have one night of passion or a months long slow-burn?", ("have one night of passion", "a months long slow-burn"), id="no-question-mark-needed"),
        pytest.param("sneeze glitter | burp bubbles", None, id="already-formatted"),
        pytest.param("Would you rather never sleep again?", None, id="no-or"),
        pytest.param("Would you rather or ?", None, id="empty-halves"),
    ],
)
def test_split_wyr(text, expected):
    assert p0.split_wyr(text) == expected


def _seed(db: Path, *, week1_kickoff: float) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE survivor_seasons (id INTEGER PRIMARY KEY, guild_id INTEGER, name TEXT,
            status TEXT, season_year INTEGER, config TEXT);
        CREATE TABLE nfl_games (season_year INTEGER, week INTEGER, kickoff_utc TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled');
        CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL,
            value TEXT NOT NULL, PRIMARY KEY (guild_id, key));
        CREATE TABLE games_question_bank (question_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_type TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'sfw',
            question_text TEXT NOT NULL, added_by INTEGER NOT NULL DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, tags TEXT NOT NULL DEFAULT '[]',
            last_served_at TIMESTAMP);
        CREATE TABLE games_scheduled (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER,
            channel_id INTEGER, game_type TEXT, options TEXT NOT NULL DEFAULT '{}',
            announce INTEGER NOT NULL DEFAULT 0, announce_role_id INTEGER);
        CREATE TABLE games_game_config (guild_id INTEGER, game_type TEXT,
            enabled INTEGER NOT NULL DEFAULT 1, options TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP, PRIMARY KEY (guild_id, game_type));
        """
    )
    # The prod shape (row 7): a legacy photo row announcing with a role while
    # the photo config carries no ping role; a second guild's row is clean;
    # a third guild's row announces with no config row at all.
    conn.executemany(
        "INSERT INTO games_scheduled (id, guild_id, channel_id, game_type, options, announce, "
        "announce_role_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (7, 1, 50, "photo", '{"prompt": ""}', 1, 777),
            (8, 2, 51, "photo", "{}", 0, None),
            (9, 3, 52, "photo", "{}", 1, 888),
            (10, 1, 53, "wyr", "{}", 1, 999),
        ],
    )
    conn.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled, options) VALUES "
        "(1, 'photo', 1, '{\"channel_id\": \"50\", \"ping_role_id\": \"\"}')"
    )
    conn.execute(
        "INSERT INTO survivor_seasons VALUES (3, 1, 'real', 'enrolling', 2026, ?)",
        (json.dumps({"channel_id": 5, "last_slate_week": 1, "last_lastcall_week": 1}),),
    )
    conn.execute(
        "INSERT INTO survivor_seasons VALUES (2, 1, 'done', 'complete', 2026, ?)",
        (json.dumps({"last_slate_week": 4, "last_lastcall_week": 4}),),
    )
    iso = datetime.fromtimestamp(week1_kickoff, tz=timezone.utc).isoformat()
    conn.execute("INSERT INTO nfl_games (season_year, week, kickoff_utc) VALUES (2026, 1, ?)", (iso,))
    conn.executemany(
        "INSERT INTO config VALUES (?, ?, ?)",
        [(1, "mahjong_duel_wall_trim", "60"), (1, "mahjong_fill_bots", "1"),
         (1, "mahjong_short_deck_rank", "5"), (2, "mahjong_fill_bots", "0")],
    )
    conn.executemany(
        "INSERT INTO games_question_bank (game_type, question_text, tags) VALUES (?, ?, ?)",
        [
            ("price", "spicy", '["Nsfw"]'),
            ("clapback", "fine", '["nsfw"]'),
            ("ffa", "mixed", '["Dare", "nsfw", "dare"]'),
            ("wyr", "Would you rather sneeze glitter or burp bubbles?", "[]"),
            ("wyr", "already | split", "[]"),
            ("wyr", "Would you rather never sleep again?", "[]"),
        ],
    )
    conn.commit()
    conn.close()


def _q(db: Path, sql: str, *params):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_dry_run_writes_nothing(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, week1_kickoff=time.time() + 5 * 86400)
    before = _q(db, "SELECT config FROM survivor_seasons") + _q(db, "SELECT value FROM config") + _q(db, "SELECT question_text, tags FROM games_question_bank")
    changed = p0.run_steps(db, list(p0.STEPS), apply=False)
    after = _q(db, "SELECT config FROM survivor_seasons") + _q(db, "SELECT value FROM config") + _q(db, "SELECT question_text, tags FROM games_question_bank")
    assert changed == 1 + 2 + 2 + 1 + 2
    assert before == after


def test_apply_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, week1_kickoff=time.time() + 5 * 86400)
    assert p0.run_steps(db, list(p0.STEPS), apply=True) == 8
    cfg = json.loads(_q(db, "SELECT config FROM survivor_seasons WHERE id = 3")[0][0])
    assert (cfg["last_slate_week"], cfg["last_lastcall_week"], cfg["channel_id"]) == (0, 0, 5)
    done = json.loads(_q(db, "SELECT config FROM survivor_seasons WHERE id = 2")[0][0])
    assert done["last_slate_week"] == 4, "a complete season is left alone"
    assert dict((k, v) for k, v in _q(db, "SELECT key, value FROM config WHERE guild_id = 1")) == {
        "mahjong_duel_wall_trim": "0", "mahjong_fill_bots": "0", "mahjong_short_deck_rank": "5",
    }
    rows = {t: (tx, tg) for t, tx, tg in _q(db, "SELECT game_type, question_text, tags FROM games_question_bank")}
    assert json.loads(rows["price"][1]) == ["nsfw"]
    assert json.loads(rows["ffa"][1]) == ["dare", "nsfw"]
    wyr = [r[0] for r in _q(db, "SELECT question_text FROM games_question_bank WHERE game_type='wyr' ORDER BY question_id")]
    assert wyr == ["sneeze glitter | burp bubbles", "already | split", "Would you rather never sleep again?"]
    sched = {r[0]: r[1:] for r in _q(db, "SELECT id, announce, announce_role_id, options FROM games_scheduled")}
    assert sched[7] == (0, None, "{}")
    assert sched[9] == (0, None, "{}")
    assert sched[10] == (1, 999, "{}"), "a non-photo row is left alone"
    cfg = {g: json.loads(o) for g, o in _q(db, "SELECT guild_id, options FROM games_game_config WHERE game_type='photo'")}
    assert cfg[1] == {"channel_id": "50", "ping_role_id": "777"}, "the role moved into the panel's dial"
    assert cfg[3]["ping_role_id"] == "888", "a guild with no config row gets one"
    assert p0.run_steps(db, list(p0.STEPS), apply=True) == 0, "second run is a no-op"


def test_photo_step_never_overwrites_a_ping_role_the_panel_already_has(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, week1_kickoff=time.time() + 5 * 86400)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE games_game_config SET options = '{\"channel_id\": \"50\", \"ping_role_id\": \"123\"}' "
        "WHERE guild_id = 1"
    )
    conn.commit()
    conn.close()
    assert p0.run_steps(db, ["photo"], apply=True) == 2
    cfg = json.loads(_q(db, "SELECT options FROM games_game_config WHERE guild_id = 1")[0][0])
    assert cfg["ping_role_id"] == "123"
    assert _q(db, "SELECT announce FROM games_scheduled WHERE id = 7") == [(0,)]


def test_survivor_reset_skips_a_season_whose_week_1_has_kicked_off(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, week1_kickoff=time.time() - 3600)
    assert p0.run_steps(db, ["survivor"], apply=True) == 0
    cfg = json.loads(_q(db, "SELECT config FROM survivor_seasons WHERE id = 3")[0][0])
    assert cfg["last_slate_week"] == 1
