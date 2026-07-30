"""Tests for /api/config/games-* — the six duel-game config endpoints.

These routes had **no coverage at all** before this file (Stage 1 of
docs/plans/config-routes-simplify.md). They are six near-identical handlers
sharing a five-field "shared tier" (cooldown/sentence/allowlist/two length
caps) plus a per-game tier, so everything below is one parametrized function
per behaviour rather than six copies of each.

The clamp floors asserted here are the contract Stage 4 has to preserve when
it collapses the six copy-pasted handlers into a shared builder.
"""

from __future__ import annotations

import pytest


# route suffix, GET /config section key, duel_config.game_type
GAMES = [
    ("games-pressure", "games_pressure", "pressure"),
    ("games-quickdraw", "games_quickdraw", "quickdraw"),
    ("games-hot-potato", "games_hot_potato", "hot_potato"),
    ("games-hot-potato-group", "games_hot_potato_group", "hot_potato_group"),
    ("games-chicken", "games_chicken", "chicken"),
    ("games-musical-chairs", "games_musical_chairs", "musical_chairs"),
]

_GAME_IDS = [g[2] for g in GAMES]


def _section(client, key: str) -> dict:
    resp = client.get("/api/config")
    assert resp.status_code == 200
    return resp.json()[key]


# ── shared tier: present and round-trips on all six games ─────────────


@pytest.mark.parametrize(("route", "key", "_game"), GAMES, ids=_GAME_IDS)
def test_shared_tier_round_trips(authed_client, route, key, _game):
    """Every game surfaces the same five shared knobs, and a PUT is readable
    back through GET /config."""
    resp = authed_client.put(
        f"/api/config/{route}",
        json={
            "cooldown_hours": 12,
            "sentence_hours": 6,
            "max_nick_length": 20,
            "max_stakes_length": 500,
        },
    )
    assert resp.status_code == 200

    sec = _section(authed_client, key)
    assert sec["cooldown_hours"] == 12
    assert sec["sentence_hours"] == 6
    assert sec["max_nick_length"] == 20
    assert sec["max_stakes_length"] == 500


@pytest.mark.parametrize(("route", "key", "_game"), GAMES, ids=_GAME_IDS)
def test_shared_tier_defaults_before_any_write(authed_client, route, key, _game):
    """With no row written, the section reports the shared defaults rather
    than erroring or omitting keys."""
    sec = _section(authed_client, key)
    assert sec["cooldown_hours"] == 48
    assert sec["sentence_hours"] == 24
    assert sec["max_nick_length"] == 32
    assert sec["max_stakes_length"] == 200
    assert sec["channel_allowlist"] == []


@pytest.mark.parametrize(("route", "key", "_game"), GAMES, ids=_GAME_IDS)
def test_partial_update_leaves_other_shared_fields_alone(
    authed_client, route, key, _game
):
    """Every field is optional; sending one must not reset the others."""
    authed_client.put(f"/api/config/{route}", json={"cooldown_hours": 9})
    authed_client.put(f"/api/config/{route}", json={"sentence_hours": 3})

    sec = _section(authed_client, key)
    assert sec["cooldown_hours"] == 9
    assert sec["sentence_hours"] == 3


# ── shared tier clamps ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "sent", "expected"),
    [
        pytest.param("cooldown_hours", -5, 0, id="cooldown-floor-0"),
        pytest.param("cooldown_hours", 0, 0, id="cooldown-allows-0"),
        pytest.param("sentence_hours", 0, 1, id="sentence-floor-1"),
        pytest.param("sentence_hours", -99, 1, id="sentence-floor-negative"),
        pytest.param("max_nick_length", 0, 1, id="nick-floor-1"),
        pytest.param("max_nick_length", 999, 32, id="nick-ceiling-32"),
        pytest.param("max_stakes_length", 0, 1, id="stakes-floor-1"),
        pytest.param("max_stakes_length", 99_999, 2000, id="stakes-ceiling-2000"),
    ],
)
def test_shared_tier_clamps(authed_client, field, sent, expected):
    resp = authed_client.put("/api/config/games-pressure", json={field: sent})
    assert resp.status_code == 200
    assert _section(authed_client, "games_pressure")[field] == expected


@pytest.mark.parametrize(("route", "key", "_game"), GAMES, ids=_GAME_IDS)
def test_channel_allowlist_dedupes_sorts_and_drops_blanks(
    authed_client, route, key, _game
):
    """The allowlist is stored as sorted, de-duplicated JSON and comes back as
    strings (snowflake-precision rule)."""
    resp = authed_client.put(
        f"/api/config/{route}",
        json={"channel_allowlist": ["300", "100", "200", "100", "  ", ""]},
    )
    assert resp.status_code == 200

    allowlist = _section(authed_client, key)["channel_allowlist"]
    assert allowlist == ["100", "200", "300"]
    assert all(isinstance(v, str) for v in allowlist)


def test_channel_allowlist_survives_snowflake_sized_ids(authed_client):
    """Ids above 2^53 must not come back as bare JSON numbers."""
    big = "1469491362444480666"
    authed_client.put(
        "/api/config/games-pressure", json={"channel_allowlist": [big]}
    )
    assert _section(authed_client, "games_pressure")["channel_allowlist"] == [big]


# ── per-game tier ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("route", "key", "payload", "expected"),
    [
        pytest.param(
            "games-quickdraw",
            "games_quickdraw",
            {"min_delay": 2.5, "max_delay": 9.0, "draw_window": 4.0},
            {"min_delay": 2.5, "max_delay": 9.0, "draw_window": 4.0},
            id="quickdraw",
        ),
        pytest.param(
            "games-hot-potato",
            "games_hot_potato",
            {"min_timer": 15.0, "max_timer": 50.0},
            {"min_timer": 15.0, "max_timer": 50.0},
            id="hot_potato",
        ),
        pytest.param(
            "games-hot-potato-group",
            "games_hot_potato_group",
            {
                "min_fuse": 25.0,
                "max_fuse": 70.0,
                "min_hold": 3.0,
                "min_players": 4,
                "max_players": 12,
            },
            {
                "min_fuse": 25.0,
                "max_fuse": 70.0,
                "min_hold": 3.0,
                "min_players": 4,
                "max_players": 12,
            },
            id="hot_potato_group",
        ),
        pytest.param(
            "games-chicken",
            "games_chicken",
            {"climb_duration": 30.0, "min_players": 3, "max_players": 9},
            {"climb_duration": 30.0, "min_players": 3, "max_players": 9},
            id="chicken",
        ),
        pytest.param(
            "games-musical-chairs",
            "games_musical_chairs",
            {
                "min_music": 6.0,
                "max_music": 18.0,
                "scramble_window": 9.0,
                "min_players": 4,
                "max_players": 12,
            },
            {
                "min_music": 6.0,
                "max_music": 18.0,
                "scramble_window": 9.0,
                "min_players": 4,
                "max_players": 12,
            },
            id="musical_chairs",
        ),
    ],
)
def test_per_game_tier_round_trips(authed_client, route, key, payload, expected):
    resp = authed_client.put(f"/api/config/{route}", json=payload)
    assert resp.status_code == 200

    sec = _section(authed_client, key)
    for field, want in expected.items():
        assert sec[field] == want, field


@pytest.mark.parametrize(
    ("route", "key", "field", "sent", "expected"),
    [
        pytest.param(
            "games-quickdraw", "games_quickdraw", "min_delay", 0.1, 0.5,
            id="quickdraw-min_delay-floor",
        ),
        pytest.param(
            "games-quickdraw", "games_quickdraw", "max_delay", 0.1, 1.0,
            id="quickdraw-max_delay-floor",
        ),
        pytest.param(
            "games-quickdraw", "games_quickdraw", "draw_window", 0.0, 1.0,
            id="quickdraw-draw_window-floor",
        ),
        pytest.param(
            "games-hot-potato", "games_hot_potato", "min_timer", 1.0, 5.0,
            id="hot_potato-min_timer-floor",
        ),
        pytest.param(
            "games-hot-potato", "games_hot_potato", "max_timer", 1.0, 10.0,
            id="hot_potato-max_timer-floor",
        ),
        pytest.param(
            "games-hot-potato-group", "games_hot_potato_group", "min_fuse", 1.0, 5.0,
            id="hp_group-min_fuse-floor",
        ),
        pytest.param(
            "games-hot-potato-group", "games_hot_potato_group", "max_fuse", 1.0, 10.0,
            id="hp_group-max_fuse-floor",
        ),
        pytest.param(
            "games-hot-potato-group", "games_hot_potato_group", "min_hold", -1.0, 0.0,
            id="hp_group-min_hold-floor",
        ),
        pytest.param(
            "games-hot-potato-group", "games_hot_potato_group", "min_players", 1, 2,
            id="hp_group-min_players-floor",
        ),
        pytest.param(
            "games-hot-potato-group", "games_hot_potato_group", "max_players", 0, 2,
            id="hp_group-max_players-floor",
        ),
        pytest.param(
            "games-chicken", "games_chicken", "climb_duration", 1.0, 5.0,
            id="chicken-climb_duration-floor",
        ),
        pytest.param(
            "games-chicken", "games_chicken", "min_players", 1, 2,
            id="chicken-min_players-floor",
        ),
        pytest.param(
            "games-chicken", "games_chicken", "max_players", 0, 2,
            id="chicken-max_players-floor",
        ),
        pytest.param(
            "games-musical-chairs", "games_musical_chairs", "min_music", 0.5, 2.0,
            id="mc-min_music-floor",
        ),
        pytest.param(
            "games-musical-chairs", "games_musical_chairs", "max_music", 0.5, 3.0,
            id="mc-max_music-floor",
        ),
        pytest.param(
            "games-musical-chairs", "games_musical_chairs", "scramble_window", 0.5, 2.0,
            id="mc-scramble_window-floor",
        ),
        pytest.param(
            "games-musical-chairs", "games_musical_chairs", "min_players", 1, 3,
            id="mc-min_players-floor",
        ),
        pytest.param(
            "games-musical-chairs", "games_musical_chairs", "max_players", 1, 3,
            id="mc-max_players-floor",
        ),
    ],
)
def test_per_game_clamps(authed_client, route, key, field, sent, expected):
    resp = authed_client.put(f"/api/config/{route}", json={field: sent})
    assert resp.status_code == 200
    assert _section(authed_client, key)[field] == expected


@pytest.mark.parametrize(
    ("sent", "expected"),
    [pytest.param(True, 1, id="true"), pytest.param(False, 0, id="false")],
)
def test_musical_chairs_false_start_elim_stores_as_int(authed_client, sent, expected):
    """The only bool in the six games — persisted as 1/0."""
    resp = authed_client.put(
        "/api/config/games-musical-chairs", json={"false_start_elim": sent}
    )
    assert resp.status_code == 200
    sec = _section(authed_client, "games_musical_chairs")
    assert sec["false_start_elim"] == expected


# ── isolation between games ───────────────────────────────────────────


def test_shared_tier_is_per_game_not_global(authed_client):
    """duel_config is keyed by (guild_id, game_type) — editing one game's
    cooldown must not move another's."""
    authed_client.put("/api/config/games-pressure", json={"cooldown_hours": 5})
    authed_client.put("/api/config/games-chicken", json={"cooldown_hours": 11})

    assert _section(authed_client, "games_pressure")["cooldown_hours"] == 5
    assert _section(authed_client, "games_chicken")["cooldown_hours"] == 11
    # A game never written to still reports the default.
    assert _section(authed_client, "games_quickdraw")["cooldown_hours"] == 48
