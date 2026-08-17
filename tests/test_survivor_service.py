"""Tests for services/survivor_service.py over the real migration schema.

Stage 1 of docs/plans/survivor.md: season lifecycle, config validation, the
flavor corpus, roster admin actions, and the erasure path. Game logic (locks,
settle, gauntlet) arrives in later stages with its own suites.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.privacy_service import purge_user_data
from bot_modules.services.survivor_service import (
    DEFAULT_CONFIG,
    DEFAULT_FLAVOR,
    FLAVOR_CATEGORIES,
    SeasonError,
    add_flavor,
    add_player,
    create_season,
    delete_flavor,
    eliminate_player,
    end_season,
    get_active_season,
    get_config,
    get_season,
    list_flavor,
    list_players,
    list_seasons,
    revive_player,
    seed_default_flavor,
    set_season_status,
    update_config,
    update_flavor,
    validate_config,
)
from tests.db_template import migrated_db

GID, GID2 = 100, 200
NOW = 1_787_000_000.0


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


# ── season lifecycle ───────────────────────────────────────────────────


def test_create_season_defaults(db):
    with open_db(db) as conn:
        season_id = create_season(conn, GID, "The Long Autumn", 2026)
        season = get_season(conn, season_id)
    assert season is not None
    assert season["status"] == "enrolling"
    assert season["guild_id"] == GID
    # Sparse storage merges over defaults — season-one decisions included.
    assert season["config"] == DEFAULT_CONFIG
    assert season["config"]["pot_seed"] == 10000
    assert season["config"]["ghost_pot_pct"] == 20
    assert season["config"]["buyin_coins"] == 0


def test_one_live_season_per_guild(db):
    with open_db(db) as conn:
        first = create_season(conn, GID, "One", 2026)
        with pytest.raises(SeasonError):
            create_season(conn, GID, "Two", 2026)
        # A second guild is independent (spec §6.12 is per-guild).
        create_season(conn, GID2, "Elsewhere", 2026)
        # Active (not just enrolling) still blocks a second create.
        set_season_status(conn, first, "active")
        with pytest.raises(SeasonError):
            create_season(conn, GID, "Three", 2026)
        # Archiving frees the slot.
        end_season(conn, first)
        assert get_active_season(conn, GID) is None
        second = create_season(conn, GID, "Two", 2027)
        assert get_active_season(conn, GID)["id"] == second
        assert [s["id"] for s in list_seasons(conn, GID)] == [second, first]


@pytest.mark.parametrize("bad_name", ["", "   "])
def test_create_season_rejects_blank_name(db, bad_name):
    with open_db(db) as conn:
        with pytest.raises(SeasonError):
            create_season(conn, GID, bad_name, 2026)


def test_schema_backstop_blocks_second_live_season(db):
    # The check-then-insert race: two concurrent creates both pass the
    # get_active_season check before either commits. The partial unique index
    # is the backstop — a direct second live INSERT must fail at the schema.
    with open_db(db) as conn:
        create_season(conn, GID, "First", 2026)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO survivor_seasons (guild_id, name, season_year) "
                "VALUES (?, 'Racer', 2026)",
                (GID,),
            )
        # A complete season doesn't occupy the slot; a second archive is fine.
        conn.execute(
            "INSERT INTO survivor_seasons (guild_id, name, season_year, status) "
            "VALUES (?, 'Old', 2025, 'complete')",
            (GID,),
        )


def test_create_season_translates_race_loss_to_season_error(db, monkeypatch):
    # The service half of the same fix: when the check misses (simulated by
    # blinding it) the IntegrityError surfaces as the same friendly SeasonError
    # the check raises, not a 500.
    import bot_modules.services.survivor_service as svc

    with open_db(db) as conn:
        create_season(conn, GID, "First", 2026)
        monkeypatch.setattr(svc, "get_active_season", lambda conn, gid: None)
        with pytest.raises(SeasonError, match="already has a live season"):
            create_season(conn, GID, "Racer", 2026)


def test_status_never_goes_backward(db):
    with open_db(db) as conn:
        season_id = create_season(conn, GID, "S", 2026)
        set_season_status(conn, season_id, "active")
        with pytest.raises(SeasonError):
            set_season_status(conn, season_id, "enrolling")
        end_season(conn, season_id)
        with pytest.raises(SeasonError):
            set_season_status(conn, season_id, "active")


def test_corrupt_stored_config_falls_back_to_defaults(db):
    with open_db(db) as conn:
        season_id = create_season(conn, GID, "S", 2026)
        conn.execute(
            "UPDATE survivor_seasons SET config = 'not json' WHERE id = ?",
            (season_id,),
        )
        assert get_season(conn, season_id)["config"] == DEFAULT_CONFIG


# ── config validation ──────────────────────────────────────────────────


def test_update_config_persists_sparsely(db):
    with open_db(db) as conn:
        season_id = create_season(conn, GID, "S", 2026)
        merged = update_config(conn, season_id, {"strikes": 2, "tie_rule": "survive"})
        assert merged["strikes"] == 2
        assert merged["tie_rule"] == "survive"
        # Only the touched keys are stored; the rest read through defaults.
        raw = json.loads(
            conn.execute(
                "SELECT config FROM survivor_seasons WHERE id = ?", (season_id,)
            ).fetchone()["config"]
        )
        assert set(raw) == {"strikes", "tie_rule"}
        assert get_season(conn, season_id)["config"]["strikes"] == 2


@pytest.mark.parametrize(
    "updates",
    [
        pytest.param({"nonsense_key": 1}, id="unknown-key"),
        pytest.param({"tie_rule": "coinflip"}, id="bad-enum"),
        pytest.param({"strikes": 3}, id="above-range"),
        pytest.param({"ghost_pot_pct": -1}, id="below-range"),
        pytest.param({"strikes": "1"}, id="stringly-int"),
        pytest.param({"strikes": True}, id="bool-not-int"),
        pytest.param({"ghost_streak": 1}, id="int-not-bool"),
        pytest.param({"slate_hour": 24}, id="hour-out-of-day"),
    ],
)
def test_validate_config_rejects(updates):
    with pytest.raises(SeasonError):
        validate_config(updates)


def test_every_default_key_validates_at_its_default():
    # The sweep the staged-config memory demands: every key in the defaults
    # dict must survive its own validation, so no key can drift unreadable.
    assert validate_config(dict(DEFAULT_CONFIG)) == DEFAULT_CONFIG


def test_get_config_ignores_unknown_stored_keys():
    # A key retired from a future version's defaults must not resurface.
    merged = get_config({"strikes": 0, "retired_dial": 99})
    assert merged["strikes"] == 0
    assert "retired_dial" not in merged


# ── roster ─────────────────────────────────────────────────────────────


def test_roster_lifecycle(db):
    with open_db(db) as conn:
        season_id = create_season(conn, GID, "S", 2026)
        assert add_player(conn, season_id, 1, joined_at=NOW)
        # One entry per person (spec §1.1) — the second join is a no-op.
        assert not add_player(conn, season_id, 1, joined_at=NOW + 60)
        add_player(conn, season_id, 2, joined_at=NOW)

        assert eliminate_player(conn, season_id, 1, week=7)
        players = {p["user_id"]: p for p in list_players(conn, season_id)}
        assert players[1]["status"] == "ghost"
        assert players[1]["eliminated_week"] == 7
        assert players[2]["status"] == "alive"

        # Eliminating a ghost (or a stranger) is a refusal, not a crash.
        assert not eliminate_player(conn, season_id, 1, week=8)
        assert not eliminate_player(conn, season_id, 999, week=8)

        assert revive_player(conn, season_id, 1)
        players = {p["user_id"]: p for p in list_players(conn, season_id)}
        assert players[1]["status"] == "alive"
        assert players[1]["eliminated_week"] is None
        # Reviving the living is likewise a refusal.
        assert not revive_player(conn, season_id, 2)


# ── flavor corpus ──────────────────────────────────────────────────────


def test_seed_flavor_idempotent_and_guild_scoped(db):
    with open_db(db) as conn:
        expected = sum(len(v) for v in DEFAULT_FLAVOR.values())
        assert seed_default_flavor(conn, GID) == expected
        # Second seed is a no-op — a curated corpus is never diluted.
        assert seed_default_flavor(conn, GID) == 0
        # Even a fully-retired corpus blocks re-seeding.
        conn.execute("UPDATE survivor_flavor SET active = 0 WHERE guild_id = ?", (GID,))
        assert seed_default_flavor(conn, GID) == 0
        # The other guild seeds independently.
        assert seed_default_flavor(conn, GID2) == expected
        assert len(list_flavor(conn, GID2)) == expected
        assert {f["category"] for f in list_flavor(conn, GID2)} == set(
            FLAVOR_CATEGORIES
        )


def test_flavor_crud_and_guild_isolation(db):
    with open_db(db) as conn:
        line_id = add_flavor(conn, GID, "eulogy", "  {name} fell.  ")
        (row,) = list_flavor(conn, GID, "eulogy")
        assert row["line"] == "{name} fell."  # stripped

        # The other guild can neither see nor touch it.
        assert list_flavor(conn, GID2) == []
        assert not update_flavor(conn, GID2, line_id, active=False)
        assert not delete_flavor(conn, GID2, line_id)

        # Retire → hidden by default, visible with include_inactive.
        assert update_flavor(conn, GID, line_id, active=False)
        assert list_flavor(conn, GID, "eulogy") == []
        assert len(list_flavor(conn, GID, "eulogy", include_inactive=True)) == 1

        assert update_flavor(conn, GID, line_id, line="{name} rose.", active=True)
        (row,) = list_flavor(conn, GID, "eulogy")
        assert row["line"] == "{name} rose."

        assert delete_flavor(conn, GID, line_id)
        assert list_flavor(conn, GID, "eulogy", include_inactive=True) == []


def test_update_flavor_with_no_fields_is_an_error_not_a_missing_row(db):
    # Regression: an empty update returned the same False as a missing row,
    # which the route translated to a 404 for a line that exists.
    with open_db(db) as conn:
        line_id = add_flavor(conn, GID, "toll", "week {week} tolls.")
        with pytest.raises(SeasonError, match="Nothing to update"):
            update_flavor(conn, GID, line_id)
        # The line is untouched.
        (row,) = list_flavor(conn, GID, "toll")
        assert row["line"] == "week {week} tolls."


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda conn: add_flavor(conn, GID, "limerick", "x"), id="add"),
        pytest.param(lambda conn: list_flavor(conn, GID, "limerick"), id="list"),
    ],
)
def test_flavor_rejects_unknown_category(db, call):
    with open_db(db) as conn:
        with pytest.raises(SeasonError):
            call(conn)


def test_flavor_slots_are_well_formed():
    # Renderers fill {name}/{team}/{week} by str.replace; a typo'd slot would
    # ship verbatim into a Reckoning, so pin the vocabulary here.
    import re

    for lines in DEFAULT_FLAVOR.values():
        for line in lines:
            assert set(re.findall(r"\{(\w+)\}", line)) <= {"name", "team", "week"}, line


# ── erasure ────────────────────────────────────────────────────────────


def test_purge_clears_survivor_rows_guild_scoped(db):
    with open_db(db) as conn:
        season_a = create_season(conn, GID, "A", 2026)
        season_b = create_season(conn, GID2, "B", 2026)
        for season_id in (season_a, season_b):
            add_player(conn, season_id, 1, joined_at=NOW)
            conn.execute(
                "INSERT INTO survivor_picks "
                "(season_id, user_id, week, slot, team, game_id) "
                "VALUES (?, 1, 1, 1, 'SF', 'g1')",
                (season_id,),
            )
        # Archived seasons are purged too — erasure ignores season status.
        end_season(conn, season_a)

        purge_user_data(conn, GID, 1)

        remaining = conn.execute(
            "SELECT season_id FROM survivor_players WHERE user_id = 1"
        ).fetchall()
        assert [r["season_id"] for r in remaining] == [season_b]
        remaining = conn.execute(
            "SELECT season_id FROM survivor_picks WHERE user_id = 1"
        ).fetchall()
        assert [r["season_id"] for r in remaining] == [season_b]
