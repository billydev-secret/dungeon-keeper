"""Mahjong dashboard routes — stage 7 of docs/plans/meadow-mahjong.md.

Config round-trip with the staged-key sweep (every key the PUT writes has a
reader in MahjongSettings), the upload gate reporting every lint problem
inline, the one-active-card demotion, the member-tier card viewer (plan D6:
readable without admin), snowflake-as-string discipline, and the report
shape."""

from __future__ import annotations

import json

from bot_modules.core.db_utils import open_db
from bot_modules.games.mahjong.card_logic import FIRST_LIGHT_PATH
from web_server.auth import SESSION_COOKIE, DiscordOAuthAuth
from web_server.server import create_app

from fastapi.testclient import TestClient


def member_client(fake_ctx):
    """A session with NO admin bits — the card-viewer tier."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app)
    cookie = auth.create_session_cookie(
        user_id=42, username="member", access_token="t",
        permission_bits=0,
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "G", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    return client


def first_light() -> dict:
    return json.loads(FIRST_LIGHT_PATH.read_text(encoding="utf-8"))


def upload_and_activate(client) -> int:
    r = client.post("/api/mahjong/cards", json={"card": first_light()})
    assert r.status_code == 200 and r.json()["ok"], r.text
    row_id = r.json()["row_id"]
    r = client.post(f"/api/mahjong/cards/{row_id}/status", json={"status": "active"})
    assert r.status_code == 200
    return row_id


def test_config_round_trip_and_staged_key_sweep(authed_client):
    body = {
        "enabled": True, "claim_window_4": 10, "claim_window_2": 5,
        "turn_timer": 30, "phase_timer": 90, "duel_wall_trim": 60,
        "second_charleston": False, "stakes_allowed": [1, 5],
        "assist_default": "coach",
    }
    r = authed_client.put("/api/mahjong/config", json=body)
    assert r.status_code == 200, r.text
    got = authed_client.get("/api/mahjong/config").json()["settings"]
    assert got["enabled"] is True
    assert got["turn_timer"] == 30.0
    assert got["duel_wall_trim"] == 60
    assert got["second_charleston"] is False
    assert got["stakes_allowed"] == [1, 5]
    assert got["assist_default"] == "coach"
    # staged-config sweep: every key the PUT writes has a dataclass reader —
    # the settings payload echoes exactly the fields the PUT accepts
    assert set(got) == set(body)


def test_config_rejects_out_of_bounds(authed_client):
    bad = {
        "enabled": True, "claim_window_4": 1, "claim_window_2": 5,
        "turn_timer": 30, "phase_timer": 90, "duel_wall_trim": 0,
        "second_charleston": True, "stakes_allowed": [1],
        "assist_default": "gap",
    }
    assert authed_client.put("/api/mahjong/config", json=bad).status_code == 422
    bad["claim_window_4"] = 8
    bad["assist_default"] = "banana"
    assert authed_client.put("/api/mahjong/config", json=bad).status_code == 422
    bad["assist_default"] = "gap"
    bad["stakes_allowed"] = [0]
    assert authed_client.put("/api/mahjong/config", json=bad).status_code == 400


def test_card_upload_reports_every_lint_problem_inline(authed_client):
    bad = {"card_id": "x", "display_name": "X", "season": "s", "hands": [
        {"id": "h1", "section": "S", "name": "N", "concealed": False,
         "value": 99, "groups": [{"count": 3, "rank": "K", "suit": "a"},
                                 {"count": 2, "rank": "5"}]},
    ]}
    r = authed_client.post("/api/mahjong/cards", json={"card": bad})
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is False
    assert len(payload["errors"]) == 2  # both problems, not just the first


def test_one_active_card_demotes_the_previous(authed_client):
    first = upload_and_activate(authed_client)
    second_data = first_light()
    second_data["card_id"] = "second"
    r = authed_client.post("/api/mahjong/cards", json={"card": second_data})
    second = r.json()["row_id"]
    r = authed_client.post(f"/api/mahjong/cards/{second}/status", json={"status": "active"})
    assert r.status_code == 200
    cards = {c["row_id"]: c["status"] for c in
             authed_client.get("/api/mahjong/config").json()["cards"]}
    assert cards[second] == "active" and cards[first] == "archived"


def test_scheduling_needs_a_future_time(authed_client):
    row_id = upload_and_activate(authed_client)
    r = authed_client.post(
        f"/api/mahjong/cards/{row_id}/status",
        json={"status": "scheduled", "activate_at": 100.0})
    assert r.status_code == 400


def test_card_viewer_is_member_readable_but_config_is_not(fake_ctx, authed_client):
    upload_and_activate(authed_client)
    client = member_client(fake_ctx)
    try:
        r = client.get("/api/mahjong/card")
        assert r.status_code == 200
        card = r.json()["card"]
        assert card is not None and card["card_id"] == "meadow-first-light"
        assert len(card["sections"]) == 7
        assert sum(len(s["hands"]) for s in card["sections"]) == 22
        # …but the admin surfaces stay closed to a plain member
        assert client.get("/api/mahjong/config").status_code == 403
        assert client.get("/api/mahjong/report").status_code == 403
        assert client.put("/api/mahjong/config", json={}).status_code in (403, 422)
    finally:
        client.close()


def test_card_viewer_with_no_active_card(authed_client):
    assert authed_client.get("/api/mahjong/card").json()["card"] is None


def test_report_shape_and_snowflake_strings(fake_ctx, authed_client):
    upload_and_activate(authed_client)
    big = 2**60 + 7  # bigger than JS Number can hold exactly
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO mahjong_tables (guild_id, channel_id, mode, stake, "
            "card_row_id, host_id, status, state, created_at, updated_at) "
            "VALUES (?, ?, 2, 1, 1, ?, 'live', '{}', 0, 0)",
            (fake_ctx.guild_id, big, big),
        )
        conn.execute(
            "INSERT INTO mahjong_results (guild_id, table_id, hand_no, mode, "
            "stake, card_id, kind, winner_id, line_id, line_name, base_value, "
            "won_by, jokerless, created_at) "
            "VALUES (?, 1, 1, 2, 1, 'c', 'mahjong', ?, 'gh-1', 'Golden Hour', "
            "25, 'discard', 1, 0)",
            (fake_ctx.guild_id, big),
        )
        conn.execute(
            "INSERT INTO mahjong_stats (guild_id, user_id, mode, hands_played, "
            "wins, jokerless_wins, coins_won, coins_lost, biggest_win) "
            "VALUES (?, ?, 2, 1, 1, 1, 100, 0, 100)",
            (fake_ctx.guild_id, big),
        )
    data = authed_client.get("/api/mahjong/report").json()
    # the serialized engine state holds every hidden rack — it must never
    # ride the report (stage-3 review, low finding #4)
    assert "state" not in data["tables"][0]
    assert data["tables"][0]["channel_id"] == str(big)
    assert data["tables"][0]["host_id"] == str(big)
    assert data["results"][0]["winner_id"] == str(big)
    assert data["aggregates"][0]["user_id"] == str(big)
