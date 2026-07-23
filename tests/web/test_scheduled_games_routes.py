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
