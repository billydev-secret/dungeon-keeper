"""Tests for /api/survivor/* — the feature's entire admin surface.

Route-layer behavior the service suite can't see: the config id
string/int round-trip (snowflake precision), the create-season two-phase
flow and its degraded role paths, and the 404/409/422 mappings. The
schedule ingest is stubbed at the module boundary — no network, ever.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import survivor_service as svc
from bot_modules.services.survivor_espn import ParsedGame

# A user id comfortably above 2^53 — JS Number would mangle it.
BIG_ID = 987654321098765432


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Stub the season sweep: creates succeed with an empty ingest unless a
    test overrides the stub with games of its own."""
    monkeypatch.setattr(
        "bot_modules.services.survivor_espn.fetch_season",
        AsyncMock(return_value=([], 0, [])),
    )


def _auth_member():
    """The session's user as a live guild member — auth re-checks perms
    against the guild whenever ctx.bot is attached (fail-closed)."""
    m = MagicMock()
    m.id = 1
    m.bot = False
    m.guild_permissions = MagicMock(value=0x8, administrator=True)
    m.display_name = "tester"
    default_role = MagicMock(id=0)
    default_role.is_default = MagicMock(return_value=True)
    m.roles = [default_role]
    return m


def _attach_bot(fake_ctx, *, roles=(), create_role=None):
    member = _auth_member()
    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.roles = list(roles)
    guild.create_role = create_role or AsyncMock(
        side_effect=lambda name, reason: MagicMock(id=hash(name) % 10**9, name=name)
    )
    guild.get_channel = MagicMock(return_value=None)
    guild.get_member = MagicMock(
        side_effect=lambda uid: member if int(uid) == 1 else None
    )
    guild.get_role = MagicMock(return_value=None)
    bot = MagicMock()
    bot.get_guild = MagicMock(
        side_effect=lambda gid: guild if gid == fake_ctx.guild_id else None
    )
    fake_ctx.bot = bot
    return guild


def _create_season(client, name="The Long Autumn", year=2026):
    return client.post(
        "/api/survivor/season", json={"name": name, "season_year": year}
    )


# ── create season ─────────────────────────────────────────────────────


def test_create_season_degraded_bot_offline(authed_client, fake_ctx, web_db):
    resp = _create_season(authed_client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["season"]["status"] == "enrolling"
    assert any("bot offline" in note for note in data["role_report"])
    assert "schedule" in data["schedule_report"]
    # The audit row is durable — written even when the Discord mirror can't be.
    with open_db(web_db) as conn:
        row = conn.execute(
            "SELECT action, actor_id FROM audit_log "
            "WHERE action = 'survivor_season_create'"
        ).fetchone()
    assert row is not None and row["actor_id"] == 1


def test_second_create_is_a_422_not_a_role_spray(authed_client, fake_ctx):
    guild = _attach_bot(fake_ctx)
    assert _create_season(authed_client).status_code == 200
    guild.create_role.reset_mock()
    resp = _create_season(authed_client, name="Second")
    assert resp.status_code == 422
    assert "already has a live season" in resp.json()["detail"]
    # The refused create must not have touched the guild's role list.
    guild.create_role.assert_not_called()


def test_create_season_creates_missing_roles_and_stores_ids(
    authed_client, fake_ctx
):
    _attach_bot(fake_ctx)
    resp = _create_season(authed_client)
    assert resp.status_code == 200
    data = resp.json()
    assert sum("created" in note for note in data["role_report"]) == 3
    config = data["season"]["config"]
    # Ids stored, and serialized as strings on the wire.
    for key in ("role_survivor_id", "role_ghost_id", "role_sole_survivor_id"):
        assert isinstance(config[key], str) and config[key] != "0"


def test_create_season_without_manage_roles_degrades(authed_client, fake_ctx):
    forbidden = discord.Forbidden(MagicMock(status=403), "Missing Permissions")
    _attach_bot(fake_ctx, create_role=AsyncMock(side_effect=forbidden))
    resp = _create_season(authed_client)
    assert resp.status_code == 200
    data = resp.json()
    assert sum("Manage Roles" in note for note in data["role_report"]) == 3
    assert data["season"]["config"]["role_survivor_id"] == "0"


def test_create_season_reports_full_slate_not_just_inserts(
    authed_client, fake_ctx, monkeypatch
):
    game = ParsedGame(
        game_id="g1", week=1, home="SEA", away="NE",
        kickoff_utc="2026-09-10T00:20:00+00:00", status="scheduled",
        favorite="SEA", favorite_prob=0.6225, winner=None,
    )
    monkeypatch.setattr(
        "bot_modules.services.survivor_espn.fetch_season",
        AsyncMock(return_value=([game], 0, [7])),
    )
    resp = _create_season(authed_client)
    assert resp.status_code == 200
    report = resp.json()["schedule_report"]
    assert "1 games, 1 new" in report
    assert "[7]" in report  # failed weeks surfaced, not swallowed


# ── config round-trip ─────────────────────────────────────────────────


def test_config_id_keys_round_trip_as_strings(authed_client, fake_ctx):
    _create_season(authed_client)
    big = str(BIG_ID)
    resp = authed_client.put(
        "/api/survivor/config", json={"channel_id": big, "strikes": 2}
    )
    assert resp.status_code == 200
    config = resp.json()["config"]
    # Snowflake precision: the exact digits survive the int round-trip.
    assert config["channel_id"] == big
    assert config["strikes"] == 2
    # And the overview serves it back the same way.
    over = authed_client.get("/api/survivor/overview").json()
    assert over["season"]["config"]["channel_id"] == big


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"channel_id": "not-a-number"}, id="unparseable-id"),
        pytest.param({"nonsense_key": 1}, id="unknown-key"),
        pytest.param({"strikes": 99}, id="out-of-range"),
    ],
)
def test_config_rejections_are_422(authed_client, fake_ctx, body):
    _create_season(authed_client)
    assert authed_client.put("/api/survivor/config", json=body).status_code == 422


def test_config_without_a_season_is_422(authed_client):
    resp = authed_client.put("/api/survivor/config", json={"strikes": 1})
    assert resp.status_code == 422


# ── overview ──────────────────────────────────────────────────────────


def test_overview_player_ids_are_strings(authed_client, fake_ctx, web_db):
    _create_season(authed_client)
    with open_db(web_db) as conn:
        season = svc.get_active_season(conn, fake_ctx.guild_id)
        svc.add_player(conn, season, BIG_ID, joined_at=1.0)
        conn.commit()
    over = authed_client.get("/api/survivor/overview").json()
    assert over["players"][0]["user_id"] == str(BIG_ID)


# ── roster ────────────────────────────────────────────────────────────


def test_eliminate_and_revive_conflict_mapping(authed_client, fake_ctx, web_db):
    _create_season(authed_client)
    with open_db(web_db) as conn:
        season = svc.get_active_season(conn, fake_ctx.guild_id)
        svc.add_player(conn, season, BIG_ID, joined_at=1.0)
        conn.commit()

    path = f"/api/survivor/player/{BIG_ID}"
    assert authed_client.post(f"{path}/eliminate", json={"week": 3}).status_code == 200
    # Already a ghost → 409, not a silent success.
    assert authed_client.post(f"{path}/eliminate", json={"week": 4}).status_code == 409
    assert authed_client.post(f"{path}/revive", json={}).status_code == 200
    assert authed_client.post(f"{path}/revive", json={}).status_code == 409
    # A stranger was never alive here.
    assert (
        authed_client.post(
            "/api/survivor/player/424242/eliminate", json={"week": 1}
        ).status_code
        == 409
    )
    # Durable audit rows for the member-affecting actions.
    with open_db(web_db) as conn:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_log WHERE target_id = ? ORDER BY id",
                (BIG_ID,),
            ).fetchall()
        ]
    assert actions == ["survivor_eliminate", "survivor_revive"]


# ── announcement ──────────────────────────────────────────────────────


def test_announcement_needs_season_then_channel(authed_client, fake_ctx):
    assert (
        authed_client.post("/api/survivor/announcement", json={}).status_code == 422
    )
    _create_season(authed_client)
    resp = authed_client.post("/api/survivor/announcement", json={})
    assert resp.status_code == 422
    assert "channel" in resp.json()["detail"].lower()


def test_announcement_bot_offline_is_503(authed_client, fake_ctx):
    _create_season(authed_client)
    authed_client.put("/api/survivor/config", json={"channel_id": "555"})
    fake_ctx.bot = None
    assert (
        authed_client.post("/api/survivor/announcement", json={}).status_code == 503
    )


def test_announcement_posts_pins_and_stores_message_id(
    authed_client, fake_ctx, monkeypatch
):
    # resolve_accent_color reads the bot avatar, which a MagicMock guild
    # can't serve — same stub voice_master's route tests use.
    monkeypatch.setattr(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=discord.Color.default()),
    )
    guild = _attach_bot(fake_ctx)
    _create_season(authed_client)
    authed_client.put("/api/survivor/config", json={"channel_id": "555"})

    message = MagicMock(id=BIG_ID)
    message.pin = AsyncMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 555
    channel.send = AsyncMock(return_value=message)
    guild.get_channel = MagicMock(
        side_effect=lambda cid: channel if int(cid) == 555 else None
    )

    resp = authed_client.post("/api/survivor/announcement", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["pinned"] is True
    assert data["message_id"] == str(BIG_ID)  # snowflake stays a string
    # Stored in config so the Join flow can refresh the counter.
    over = authed_client.get("/api/survivor/overview").json()
    assert over["season"]["config"]["announcement_message_id"] == str(BIG_ID)
    # The posted view carries the persistent Join button.
    (_, kwargs) = channel.send.call_args
    custom_ids = [item.custom_id for item in kwargs["view"].children]
    assert any(cid.startswith("survivor_join:") for cid in custom_ids)


def test_repost_retires_the_previous_announcement(
    authed_client, fake_ctx, monkeypatch
):
    # Regression (08-17 review): a repost pinned a second announcement and
    # left the old one live with a frozen counter — the setup-pin swamp.
    monkeypatch.setattr(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=discord.Color.default()),
    )
    guild = _attach_bot(fake_ctx)
    _create_season(authed_client)
    authed_client.put("/api/survivor/config", json={"channel_id": "555"})

    old_partial = MagicMock()
    old_partial.delete = AsyncMock()
    messages = iter([MagicMock(id=111, pin=AsyncMock()),
                     MagicMock(id=222, pin=AsyncMock())])
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 555
    channel.send = AsyncMock(side_effect=lambda **kw: next(messages))
    channel.get_partial_message = MagicMock(return_value=old_partial)
    guild.get_channel = MagicMock(
        side_effect=lambda cid: channel if int(cid) == 555 else None
    )

    first = authed_client.post("/api/survivor/announcement", json={})
    assert first.status_code == 200
    assert first.json()["retired_previous"] is False

    second = authed_client.post("/api/survivor/announcement", json={})
    assert second.status_code == 200
    assert second.json()["retired_previous"] is True
    channel.get_partial_message.assert_called_with(111)
    old_partial.delete.assert_awaited_once()
    over = authed_client.get("/api/survivor/overview").json()
    assert over["season"]["config"]["announcement_message_id"] == "222"


def test_eliminate_week_out_of_bounds_is_422(authed_client, fake_ctx):
    _create_season(authed_client)
    resp = authed_client.post(
        f"/api/survivor/player/{BIG_ID}/eliminate", json={"week": 99}
    )
    assert resp.status_code == 422


# ── week card + manual settle ─────────────────────────────────────────


def _seed_week(web_db, fake_ctx):
    """A tiny settled-ish week: one final-ready game, one scheduled."""
    with open_db(web_db) as conn:
        conn.execute(
            "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
            " kickoff_utc) VALUES"
            " (2026, 1, 'g-a', 'SEA', 'NE', '2026-09-10T00:20:00+00:00'),"
            " (2026, 1, 'g-b', 'KC', 'LV', '2099-09-13T17:00:00+00:00')"
        )
        season = svc.get_active_season(conn, fake_ctx.guild_id)
        svc.add_player(conn, season, BIG_ID, joined_at=1.0)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot,"
            " team, game_id) VALUES (?, ?, ?, 1, 1, 'NE', 'g-a')",
            (season["id"], season["guild_id"], BIG_ID),
        )
        conn.commit()
        return season


def test_week_card_shape(authed_client, fake_ctx, web_db):
    assert authed_client.get("/api/survivor/week").status_code == 422  # no season
    _create_season(authed_client)
    _seed_week(web_db, fake_ctx)
    data = authed_client.get("/api/survivor/week").json()
    assert data["week"] == 1
    assert {g["game_id"] for g in data["games"]} == {"g-a", "g-b"}
    assert data["alive"] == 1 and data["picked"] == 1


def test_manual_settle_grades_and_corrects(authed_client, fake_ctx, web_db):
    _create_season(authed_client)
    season = _seed_week(web_db, fake_ctx)

    resp = authed_client.post(
        "/api/survivor/settle", json={"game_id": "g-a", "outcome": "sea"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["correction"] is False
    assert data["changes"][str(season["id"])]["graded"] == 1
    with open_db(web_db) as conn:
        row = conn.execute(
            "SELECT result FROM survivor_picks WHERE season_id = ?",
            (season["id"],),
        ).fetchone()
        assert row["result"] == "loss"
        player = conn.execute(
            "SELECT strikes_used FROM survivor_players WHERE season_id = ?",
            (season["id"],),
        ).fetchone()
        assert player["strikes_used"] == 1
        audit = conn.execute(
            "SELECT extra FROM audit_log WHERE action = 'survivor_manual_settle'"
        ).fetchone()
        assert audit is not None

    # The correction path: flip the winner, the strike unwinds.
    resp = authed_client.post(
        "/api/survivor/settle", json={"game_id": "g-a", "outcome": "NE"}
    )
    assert resp.json()["correction"] is True
    with open_db(web_db) as conn:
        player = conn.execute(
            "SELECT strikes_used, status FROM survivor_players WHERE season_id = ?",
            (season["id"],),
        ).fetchone()
        assert (player["strikes_used"], player["status"]) == (0, "alive")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        pytest.param({"game_id": "g-a", "outcome": "KC"}, "Outcome must be",
                     id="wrong-team"),
        pytest.param({"game_id": "nope", "outcome": "SEA"}, "No such game",
                     id="unknown-game"),
    ],
)
def test_manual_settle_validation(authed_client, fake_ctx, web_db, body, match):
    _create_season(authed_client)
    _seed_week(web_db, fake_ctx)
    resp = authed_client.post("/api/survivor/settle", json=body)
    assert resp.status_code == 422
    assert match in resp.json()["detail"]
