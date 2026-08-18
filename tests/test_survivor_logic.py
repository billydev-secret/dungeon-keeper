"""Tests for survivor/logic.py — locks, no-reuse, joins, satchel, board.

Stage 3 of docs/plans/survivor.md. Spec §6's edge cases in scope here:
#1 (Thursday trap), #4 (byes filtered AND validated server-side),
#5 (opposing picks same game), #6 (UTC in, computed against now).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import economy_service
from bot_modules.services.survivor_service import (
    create_season,
    eliminate_player,
    end_season,
    get_season,
)
from bot_modules.survivor.logic import (
    AFC_TEAMS,
    ALL_TEAMS,
    NFC_TEAMS,
    GauntletPendingError,
    PickError,
    board_data,
    burned_teams,
    elapsed_weeks,
    join_season,
    legal_teams,
    pick_week,
    place_pick,
    player_status,
    pot_totals,
    satchel,
)
from tests.db_template import migrated_db

GID = 100
YEAR = 2026
# A fixed "now": Wednesday of week 1, well before any kickoff.
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR

# Week 1 mini-slate: a Thursday game, two Sunday games, a Monday game.
# CHI and GB are deliberately on bye (edge #4).
THU = NOW + DAY            # Thu evening
SUN = NOW + 4 * DAY        # Sunday
MON = NOW + 5 * DAY        # Monday night
WEEK1 = [
    ("g-thu", "SEA", "NE", THU),
    ("g-sun1", "SF", "ARI", SUN),
    ("g-sun2", "KC", "LV", SUN),
    ("g-mon", "PHI", "DAL", MON),
]
WEEK2 = [
    ("g2-a", "SEA", "SF", NOW + 8 * DAY),
    ("g2-b", "GB", "CHI", NOW + 11 * DAY),
]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _seed_games(conn, week: int, games) -> None:
    for gid, home, away, ts in games:
        conn.execute(
            "INSERT INTO nfl_games (season_year, week, game_id, home, away, kickoff_utc)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (YEAR, week, gid, home, away, _iso(ts)),
        )


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        _seed_games(conn, 1, WEEK1)
        _seed_games(conn, 2, WEEK2)
    return db_path


def _season(conn, **overrides) -> dict:
    season_id = create_season(conn, GID, "S", YEAR, overrides=overrides or None)
    season = get_season(conn, season_id)
    assert season is not None
    return season


def _join(conn, season, user_id, now=NOW):
    return join_season(conn, season, user_id, now)


# ── team constants ─────────────────────────────────────────────────────


def test_team_constants_match_the_real_league():
    assert len(ALL_TEAMS) == 32
    assert not (AFC_TEAMS & NFC_TEAMS)
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/espn/2026_week1_pregame.json")
        .read_text(encoding="utf-8")
    )
    seen = {
        t["team"]["abbreviation"]
        for ev in fixture["events"]
        for t in ev["competitions"][0]["competitors"]
    }
    assert seen == ALL_TEAMS  # an ESPN abbr rename breaks here first


# ── weeks ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(NOW, 1, id="wednesday-week1"),
        pytest.param(THU + HOUR, 1, id="thursday-kicked-still-week1"),
        pytest.param(MON + HOUR, 2, id="after-mnf-week2"),
        pytest.param(NOW + 12 * DAY, None, id="season-exhausted"),
    ],
)
def test_pick_week(db, now, expected):
    with open_db(db) as conn:
        assert pick_week(conn, YEAR, now) == expected


def test_elapsed_weeks_needs_every_game_kicked(db):
    with open_db(db) as conn:
        assert elapsed_weeks(conn, YEAR, NOW) == []
        # Mid-week: Thursday kicked, Sunday open → NOT elapsed (§1.9).
        assert elapsed_weeks(conn, YEAR, THU + HOUR) == []
        assert elapsed_weeks(conn, YEAR, MON + HOUR) == [1]


# ── legal teams ────────────────────────────────────────────────────────


def test_legal_teams_thursday_trap_and_byes(db):
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        before = {g.team for g in legal_teams(conn, season, 1, 1, NOW)}
        assert before == {"SEA", "NE", "SF", "ARI", "KC", "LV", "PHI", "DAL"}
        assert "CHI" not in before  # bye (edge #4)
        # Thursday kickoff: ONLY the Thursday teams leave the menu (edge #1).
        after = {g.team for g in legal_teams(conn, season, 1, 1, THU + 1)}
        assert after == before - {"SEA", "NE"}


def test_legal_teams_excludes_burned_but_void_returns(db):
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot, team, game_id, result)"
            " VALUES (?, ?, 1, 0, 1, 'SEA', 'g-old', 'loss')",
            (season["id"], GID),
        )
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot, team, game_id, result)"
            " VALUES (?, ?, 1, 0, 2, 'KC', 'g-old2', 'void')",
            (season["id"], GID),
        )
        teams = {g.team for g in legal_teams(conn, season, 1, 1, NOW)}
        assert "SEA" not in teams   # burned by a loss
        assert "KC" in teams        # void returned it (§1.3)
        assert burned_teams(conn, season["id"], 1) == {"SEA"}
        assert satchel(conn, season["id"], 1) == ALL_TEAMS - {"SEA"}


# ── place_pick ─────────────────────────────────────────────────────────


def test_pick_place_and_change_before_kickoff(db):
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        game = place_pick(conn, season, 1, 1, "SEA", NOW)
        assert (game.opponent, game.is_home) == ("NE", True)
        # Free change any time before the picked team's kickoff (§1.2).
        game = place_pick(conn, season, 1, 1, "SF", NOW + HOUR)
        assert game.game_id == "g-sun1"
        row = conn.execute(
            "SELECT team, game_id FROM survivor_picks WHERE season_id = ? AND user_id = 1",
            (season["id"],),
        ).fetchall()
        assert len(row) == 1 and row[0]["team"] == "SF"


def test_pick_locks_at_own_kickoff_even_with_games_open(db):
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        # Thursday kicked; Sunday games are still open, but THIS pick is
        # locked (§1.2 per-game locking).
        with pytest.raises(PickError, match="locked at kickoff"):
            place_pick(conn, season, 1, 1, "SF", THU + 1)


def test_pick_after_thursday_still_open_for_the_unpicked(db):
    # The other side of the trap: someone with no pick yet can still pick
    # any not-yet-kicked-off team on Friday (edge #1: nothing was missed).
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        game = place_pick(conn, season, 1, 1, "KC", THU + HOUR)
        assert game.game_id == "g-sun2"


def test_opposing_picks_same_game_are_fine(db):
    # Edge #5: two players on opposite sides of one game.
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        _join(conn, season, 2)
        assert place_pick(conn, season, 1, 1, "SEA", NOW).game_id == "g-thu"
        assert place_pick(conn, season, 2, 1, "NE", NOW).game_id == "g-thu"


@pytest.mark.parametrize(
    ("team", "match"),
    [
        pytest.param("XYZ", "Never heard of", id="unknown-team"),
        pytest.param("CHI", "isn't available", id="bye-team"),
        pytest.param("SEA", "already used", id="burned-team"),
    ],
)
def test_pick_refusals_teach_the_rule(db, team, match):
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot, team, game_id, result)"
            " VALUES (?, ?, 1, 0, 1, 'SEA', 'g-old', 'loss')",
            (season["id"], GID),
        )
        with pytest.raises(PickError, match=match):
            place_pick(conn, season, 1, 1, team, NOW)


@pytest.mark.parametrize(
    ("result", "ok"),
    [
        pytest.param("loss", False, id="settled-loss-immutable"),
        pytest.param("win", False, id="settled-win-immutable"),
        pytest.param("void", True, id="void-frees-the-slot"),
    ],
)
def test_settled_results_are_immutable_but_void_frees(db, result, ok):
    # Regression (08-17 review): a settled result on the current week's pick
    # bypassed the lock and the upsert erased it back to NULL — a graded loss
    # vanished before the Reckoning. Only 'void' frees the slot (§1.3).
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot, team, game_id, result)"
            " VALUES (?, ?, 1, 1, 1, 'SEA', 'g-thu', ?)",
            (season["id"], GID, result),
        )
        if ok:
            game = place_pick(conn, season, 1, 1, "SF", NOW)
            assert game.team == "SF"
        else:
            with pytest.raises(PickError, match="already settled"):
                place_pick(conn, season, 1, 1, "SF", NOW)
            row = conn.execute(
                "SELECT team, result FROM survivor_picks "
                "WHERE season_id = ? AND user_id = 1 AND week = 1",
                (season["id"],),
            ).fetchone()
            assert (row["team"], row["result"]) == ("SEA", result)


def test_pick_guards_membership_week_and_season_state(db):
    with open_db(db) as conn:
        season = _season(conn)
        with pytest.raises(PickError, match="use the Join button"):
            place_pick(conn, season, 1, 1, "SEA", NOW)
        _join(conn, season, 1)
        with pytest.raises(PickError, match="week 1"):
            place_pick(conn, season, 1, 2, "SEA", NOW)  # future week
        end_season(conn, season["id"])
        season = get_season(conn, season["id"])
        with pytest.raises(PickError, match="season is over"):
            place_pick(conn, season, 1, 1, "SEA", NOW)


def test_ghosts_pick_when_streak_on_and_rest_when_off(db):
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        eliminate_player(conn, season["id"], 1, week=1)
        # Identical flow for the dead (§1.7).
        assert place_pick(conn, season, 1, 1, "SEA", NOW).team == "SEA"

        # Second guild (one-live-season rule) with the streak switched off.
        season2 = get_season(conn, create_season(conn, GID + 1, "Q", YEAR,
                                                 overrides={"ghost_streak": False}))
        assert season2 is not None
        _join(conn, season2, 5)
        eliminate_player(conn, season2["id"], 5, week=1)
        with pytest.raises(PickError, match="Ghost picks are disabled"):
            place_pick(conn, season2, 5, 1, "SEA", NOW)


# ── joining ────────────────────────────────────────────────────────────


def test_join_free_season_and_duplicate(db):
    with open_db(db) as conn:
        season = _season(conn)
        assert _join(conn, season, 1).charged == 0
        with pytest.raises(PickError, match="already in"):
            _join(conn, season, 1)


def test_join_buyin_debits_through_the_ledger(db):
    with open_db(db) as conn:
        season = _season(conn, buyin_coins=100)
        economy_service.apply_credit(conn, GID, 1, 250, "test_seed")
        assert _join(conn, season, 1).charged == 100
        assert economy_service.get_balance(conn, GID, 1) == 150
        row = conn.execute(
            "SELECT amount, kind, meta FROM econ_ledger "
            "WHERE guild_id = ? AND kind = 'survivor_buyin'",
            (GID,),
        ).fetchone()
        assert row["amount"] == -100
        assert json.loads(row["meta"])["season_id"] == season["id"]


def test_join_insufficient_balance_leaves_no_entry(db):
    with open_db(db) as conn:
        season = _season(conn, buyin_coins=100)
        economy_service.apply_credit(conn, GID, 1, 40, "test_seed")
        with pytest.raises(PickError, match="balance is short"):
            _join(conn, season, 1)
        assert economy_service.get_balance(conn, GID, 1) == 40
        assert player_status(conn, season, 1, NOW) is None


def test_join_race_loser_raises_so_the_debit_rolls_back(db, monkeypatch):
    # Regression (08-17 review): two double-clicked confirms both passed the
    # duplicate SELECT; the loser's add_player INSERT OR IGNORE returned
    # False silently and the member was charged twice. The loser must raise.
    import bot_modules.survivor.logic as logic_mod

    with open_db(db) as conn:
        season = _season(conn, buyin_coins=100)
        economy_service.apply_credit(conn, GID, 1, 200, "test_seed")
        monkeypatch.setattr(logic_mod, "add_player", lambda *a, **kw: False)
        with pytest.raises(PickError, match="already in"):
            join_season(conn, season, 1, NOW)


def test_late_join_is_gauntlet_pending_or_closed(db):
    with open_db(db) as conn:
        season = _season(conn)
        with pytest.raises(GauntletPendingError):
            join_season(conn, season, 1, MON + HOUR)  # week 1 fully kicked
    with open_db(db) as conn:
        season = get_season(conn, create_season(conn, GID + 1, "C", YEAR,
                                                overrides={"late_entry": "closed"}))
        with pytest.raises(PickError, match="closed"):
            join_season(conn, season, 1, MON + HOUR)


# ── status & board ─────────────────────────────────────────────────────


def test_player_status_shape(db):
    with open_db(db) as conn:
        season = _season(conn)
        _join(conn, season, 1)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        st = player_status(conn, season, 1, NOW)
        assert st["pick"]["team"] == "SEA"
        assert not st["pick_locked"]
        assert st["satchel_count"] == 31
        assert not st["satchel_low"]
        # After the Thursday kickoff the same pick reads locked.
        st = player_status(conn, season, 1, THU + 1)
        assert st["pick_locked"]
        assert player_status(conn, season, 999, NOW) is None


def test_board_pots_and_most_burned(db):
    with open_db(db) as conn:
        season = _season(conn, buyin_coins=50)
        for uid in (1, 2, 3):
            economy_service.apply_credit(conn, GID, uid, 100, "test_seed")
            _join(conn, season, uid)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        place_pick(conn, season, 2, 1, "SEA", NOW)
        place_pick(conn, season, 3, 1, "KC", NOW)
        eliminate_player(conn, season["id"], 3, week=1)
        board = board_data(conn, season, NOW)
        # Seed 10,000 splits 8,000/2,000; three 50-coin buy-ins land in main.
        assert board["pots"] == {"main": 8150, "ghost": 2000}
        # Regression (08-17 review): unsettled picks are THIS week's secrets —
        # the public most-burned stat must not count them (§1.2).
        assert board["most_burned"] == []
        conn.execute(
            "UPDATE survivor_picks SET result = 'loss' "
            "WHERE season_id = ? AND team = 'SEA'",
            (season["id"],),
        )
        board = board_data(conn, season, NOW)
        assert board["most_burned"][0] == ("SEA", 2)
        assert [p["user_id"] for p in board["alive"]] == [1, 2]
        assert board["graveyard"][0]["user_id"] == 3
        assert board["week"] == 1


def test_pot_scoping_is_exact_not_a_prefix_match(db):
    # Regression (08-17 review): meta LIKE '%"season_id": 1%' also matched
    # season 12 — and was welded to json.dumps spacing. json_extract is
    # spacing-proof and exact.
    with open_db(db) as conn:
        season = _season(conn)  # id 1
        assert season["id"] == 1
        base = pot_totals(conn, season)["main"]
        # A different season's fee whose id has ours as a decimal prefix.
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, meta, created_at)"
            " VALUES (?, 9, -500, 'survivor_buyin', '{\"season_id\": 12}', 1)",
            (GID,),
        )
        assert pot_totals(conn, season)["main"] == base
        # Compact JSON (no space after the colon) still counts for OUR season.
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, meta, created_at)"
            " VALUES (?, 9, -70, 'survivor_buyin', '{\"season_id\":1}', 1)",
            (GID,),
        )
        assert pot_totals(conn, season)["main"] == base + 70


def test_pot_totals_are_season_scoped(db):
    with open_db(db) as conn:
        season = _season(conn, buyin_coins=50)
        economy_service.apply_credit(conn, GID, 1, 100, "test_seed")
        _join(conn, season, 1)
        end_season(conn, season["id"])
        # A fresh season in the same guild starts from the seed alone.
        fresh = _season(conn, buyin_coins=50)
        assert pot_totals(conn, fresh) == {"main": 8000, "ghost": 2000}
        old = get_season(conn, season["id"])
        assert pot_totals(conn, old)["main"] == 8050


# ── embed hardening (feature-file pins; accent riding the shared contract) ──


def test_board_embed_fields_stay_under_discord_limit():
    from bot_modules.survivor.embeds import build_board_embed

    board = {
        "week": 1,
        "alive": [
            {"user_id": i, "strikes_used": i % 2, "weeks_survived": 3}
            for i in range(60)
        ],
        "graveyard": [
            {"user_id": 100 + i, "eliminated_week": 2} for i in range(60)
        ],
        "pots": {"main": 8000, "ghost": 2000},
        "most_burned": [("SEA", 4)],
    }
    embed = build_board_embed(
        board, lambda uid: f"a-perfectly-ordinary-nickname-{uid}", season_name="S"
    )
    for field in embed.fields:
        assert field.value is not None and len(field.value) <= 1024
    alive_field = embed.fields[0]
    assert "more players" in alive_field.value  # overflow is honest, not silent


@pytest.mark.parametrize(
    ("late_entry", "needle"),
    [
        pytest.param("gauntlet", "auto-replay as the Gauntlet", id="gauntlet"),
        pytest.param("ghost_only", "Ghost Streak side game", id="ghost-only"),
        pytest.param("closed", "closes at Week 1 kickoff", id="closed"),
    ],
)
def test_announcement_late_entry_copy_matches_config(late_entry, needle):
    # Regression (08-17 review): the pin promised "join any week" over a
    # closed season — the bullet must tell the season's actual truth.
    from bot_modules.survivor.embeds import build_announcement_embed

    embed = build_announcement_embed(
        season_name="S", entrants=1, buyin=0,
        gauntlet_mode=False, late_entry=late_entry,
    )
    rules = next(f for f in embed.fields if f.name == "The Rules")
    assert needle in rules.value
