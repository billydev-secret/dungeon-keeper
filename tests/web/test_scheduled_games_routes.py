"""Integration tests for /api/games/schedule/* — id precision on list/edit."""

from __future__ import annotations

BASE = "/api/games/schedule"

# Snowflakes that don't survive as JS numbers (> 2^53, still within SQLite int64).
BIG_CHANNEL = "1234567890123456789"
BIG_ROLE = "1122334455667788990"


def _body(**over):
    body = {
        "channel_id": BIG_CHANNEL,
        "game_type": "wyr",
        "recurrence": "daily",
        "time": "20:00",
    }
    body.update(over)
    return body


def test_list_stringifies_snowflake_ids(open_client):
    # Create a schedule carrying a role ping, then confirm the list endpoint
    # returns guild/channel/role ids as strings so JS keeps full precision.
    resp = open_client.post(BASE, json=_body(announce=True, announce_role_id=BIG_ROLE))
    assert resp.status_code == 200, resp.text

    rows = open_client.get(BASE).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["channel_id"] == BIG_CHANNEL
    assert row["announce_role_id"] == BIG_ROLE
    assert isinstance(row["guild_id"], str)
    # None stays None (no role selected) rather than becoming the string "None".
    for key in ("channel_id", "announce_role_id", "guild_id"):
        assert not isinstance(row[key], int)


def test_list_leaves_null_role_as_none(open_client):
    resp = open_client.post(BASE, json=_body(announce=False))
    assert resp.status_code == 200, resp.text
    row = open_client.get(BASE).json()[0]
    assert row["announce_role_id"] is None


# ── hosting tags (discovery-4) ──────────────────────────────────────────────


def test_options_tag_every_game_with_how_it_runs(open_client):
    from bot_modules.games.constants import (
        AUTO_START_LOBBY_TYPES,
        HOSTING_LABEL,
        LOBBY_GAME_TYPES,
        SCHEDULABLE_GAME_TYPES,
    )

    data = open_client.get(f"{BASE}/options").json()
    by_type = {g["type"]: g["hosting"] for g in data["games"]}
    assert set(by_type) == set(SCHEDULABLE_GAME_TYPES)
    assert set(by_type.values()) <= set(HOSTING_LABEL) == {"self", "timer", "countdown", "host"}
    # A prompt card and a timed round run themselves; a lobby never does —
    # except the two whose cog starts them on a scheduled countdown (P4).
    assert by_type["ffa"] == "self"
    assert by_type["risky_roll"] == "self"
    assert by_type["wyr"] == "timer"
    assert by_type["clapback"] == "countdown"
    assert by_type["price"] == "countdown"
    assert all(by_type[g] == "host" for g in LOBBY_GAME_TYPES - AUTO_START_LOBBY_TYPES)
    assert data["retry_grace_seconds"] > 0


def test_self_running_games_never_open_a_lobby():
    from bot_modules.games.constants import (
        AUTO_START_LOBBY_TYPES,
        LOBBY_GAME_TYPES,
        SELF_RUNNING_GAME_TYPES,
        TIMER_RUNNING_GAME_TYPES,
    )

    assert not (SELF_RUNNING_GAME_TYPES & LOBBY_GAME_TYPES)
    assert not (TIMER_RUNNING_GAME_TYPES & LOBBY_GAME_TYPES)
    assert not (SELF_RUNNING_GAME_TYPES & TIMER_RUNNING_GAME_TYPES)
    # A countdown lobby is still a lobby — the tag says it starts itself,
    # never that it skips the join phase.
    assert AUTO_START_LOBBY_TYPES <= LOBBY_GAME_TYPES
    assert not (AUTO_START_LOBBY_TYPES & (SELF_RUNNING_GAME_TYPES | TIMER_RUNNING_GAME_TYPES))


def test_list_carries_hosting_and_last_launched(open_client):
    resp = open_client.post(BASE, json=_body(game_type="clapback"))
    assert resp.status_code == 200, resp.text
    row = open_client.get(BASE).json()[0]
    assert row["hosting"] == "countdown"
    assert row["last_launched_at"] is None  # never run yet (platform-24)
