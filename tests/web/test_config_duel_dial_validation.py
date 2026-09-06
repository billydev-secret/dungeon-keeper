"""Server-side refusal of duel/party dial pairs that make a game unplayable.

Two families of dials on the six ``/api/config/games-*`` panels are only
meaningful in one order, and until now the ordering was checked **in the
browser only** — a direct PUT, or a stale tab, saved a game nobody can play:

* ``min_hold`` >= the shortest fuse — the holder is never allowed to pass
  before the bomb goes off, so Hot Potato stops being a game of nerve.
* ``min_players`` > ``max_players`` — the lobby closes to new joins before it
  is ever allowed to start, so it can never fill.

Both are refused here, against the *effective* values (what is stored, merged
with what this save changes), because every field is independently optional.
"""

from __future__ import annotations

import pytest


def _section(client, key: str) -> dict:
    resp = client.get("/api/config")
    assert resp.status_code == 200
    return resp.json()[key]


# ── refusals ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("route", "payload", "expect_in_detail"),
    [
        pytest.param(
            "games-hot-potato",
            {"min_timer": 10.0, "max_timer": 45.0, "min_hold": 10.0},
            "Shortest Hold",
            id="hot_potato-hold-equals-fuse",
        ),
        pytest.param(
            "games-hot-potato",
            {"min_timer": 10.0, "max_timer": 45.0, "min_hold": 30.0},
            "Shortest Fuse",
            id="hot_potato-hold-longer-than-fuse",
        ),
        pytest.param(
            "games-hot-potato-group",
            {"min_fuse": 20.0, "max_fuse": 60.0, "min_hold": 20.0},
            "Must Hold For",
            id="hp_group-hold-equals-fuse",
        ),
        pytest.param(
            "games-hot-potato-group",
            {"min_players": 9, "max_players": 4},
            "Fewest Players to Start",
            id="hp_group-players-inverted",
        ),
        pytest.param(
            "games-chicken",
            {"min_players": 8, "max_players": 3},
            "Most Players Per Lobby",
            id="chicken-players-inverted",
        ),
        pytest.param(
            "games-musical-chairs",
            {"min_players": 10, "max_players": 4},
            "Fewest Players to Start",
            id="mc-players-inverted",
        ),
        # The three min/max ranges the browser has always checked, now checked
        # here too. Chicken's is the one this branch made reachable: migration
        # 208 split the single climb duration into a pair.
        pytest.param(
            "games-chicken",
            {"min_climb": 90.0, "max_climb": 30.0},
            "Earliest Crash",
            id="chicken-climb-inverted",
        ),
        pytest.param(
            "games-hot-potato",
            {"min_timer": 90.0, "max_timer": 30.0},
            "Shortest Fuse",
            id="hot_potato-fuse-inverted",
        ),
        pytest.param(
            "games-hot-potato-group",
            {"min_fuse": 90.0, "max_fuse": 30.0},
            "Shortest Fuse",
            id="hp_group-fuse-inverted",
        ),
    ],
)
def test_unplayable_dial_pair_is_refused(authed_client, route, payload, expect_in_detail):
    resp = authed_client.put(f"/api/config/{route}", json=payload)
    assert resp.status_code == 422
    assert expect_in_detail in resp.json()["detail"]


def test_refusal_does_not_write_anything(authed_client):
    """A refused save leaves every dial in the request untouched — no partial
    write of the fields that were fine."""
    authed_client.put(
        "/api/config/games-chicken", json={"min_players": 3, "max_players": 6}
    )
    resp = authed_client.put(
        "/api/config/games-chicken",
        json={"min_climb": 12.0, "min_players": 8, "max_players": 4},
    )
    assert resp.status_code == 422

    sec = _section(authed_client, "games_chicken")
    assert sec["min_players"] == 3
    assert sec["max_players"] == 6
    assert sec["min_climb"] == 10.0  # the default, i.e. the 12.0 never landed


def test_partial_save_is_judged_against_the_stored_value(authed_client):
    """Sending one half of a pair is checked against what is already saved,
    not against the default it would otherwise be compared with."""
    authed_client.put(
        "/api/config/games-hot-potato-group",
        json={"min_fuse": 20.0, "max_fuse": 60.0, "min_hold": 2.0},
    )
    resp = authed_client.put("/api/config/games-hot-potato-group", json={"min_hold": 25.0})
    assert resp.status_code == 422
    assert _section(authed_client, "games_hot_potato_group")["min_hold"] == 2.0


# ── boundaries that must still be accepted ────────────────────────────


@pytest.mark.parametrize(
    ("route", "key", "payload"),
    [
        pytest.param(
            "games-hot-potato",
            "games_hot_potato",
            {"min_timer": 10.0, "max_timer": 45.0, "min_hold": 9.0},
            id="hot_potato-hold-just-under-fuse",
        ),
        pytest.param(
            "games-hot-potato-group",
            "games_hot_potato_group",
            {"min_fuse": 20.0, "max_fuse": 60.0, "min_hold": 19.0},
            id="hp_group-hold-just-under-fuse",
        ),
        pytest.param(
            "games-hot-potato-group",
            "games_hot_potato_group",
            {"min_players": 5, "max_players": 5},
            id="hp_group-players-equal",
        ),
        pytest.param(
            "games-chicken",
            "games_chicken",
            {"min_players": 4, "max_players": 4},
            id="chicken-players-equal",
        ),
        pytest.param(
            "games-musical-chairs",
            "games_musical_chairs",
            {"min_players": 6, "max_players": 6},
            id="mc-players-equal",
        ),
    ],
)
def test_boundary_values_still_save(authed_client, route, key, payload):
    resp = authed_client.put(f"/api/config/{route}", json=payload)
    assert resp.status_code == 200
    sec = _section(authed_client, key)
    for field, value in payload.items():
        assert sec[field] == value


def test_defaults_pass_their_own_validation(authed_client):
    """Re-saving what the panel shows on a fresh guild must not be refused —
    the shipped defaults have to satisfy the rules they are validated by."""
    for route, payload in [
        ("games-hot-potato", {"min_timer": 10.0, "max_timer": 45.0, "min_hold": 2.0}),
        (
            "games-hot-potato-group",
            {
                "min_fuse": 20.0, "max_fuse": 60.0, "min_hold": 2.0,
                "min_players": 2, "max_players": 10,
            },
        ),
        ("games-chicken", {"min_players": 2, "max_players": 8}),
        ("games-musical-chairs", {"min_players": 3, "max_players": 10}),
    ]:
        assert authed_client.put(f"/api/config/{route}", json=payload).status_code == 200


def test_shared_tier_save_is_never_blocked_by_game_dials(authed_client):
    """Editing a cooldown must not trip a rule about dials the request does
    not touch."""
    resp = authed_client.put("/api/config/games-chicken", json={"cooldown_hours": 4})
    assert resp.status_code == 200
    assert _section(authed_client, "games_chicken")["cooldown_hours"] == 4


def test_rule_table_names_only_real_model_fields():
    """The rules are read out of the effective config dict by name; a typo
    would simply never fire and the refusal would silently vanish."""
    from web_server.routes import config as cfg

    for game_key, rules in cfg._DUEL_DIAL_RULES.items():
        known = set(cfg._DUEL_GAMES[game_key]["defaults"])
        for low_field, high_field, _strict, _message in rules:
            assert {low_field, high_field} <= known, (
                f"{game_key} rule names field(s) outside its config defaults"
            )
