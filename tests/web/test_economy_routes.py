"""Tests for /api/economy/config endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import load_econ_settings
from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE
from web_server.server import create_app


def _seed_rollup(fake_ctx, iso_week: str, *, median: float, **cols) -> None:
    """Insert one econ_metrics_weekly row for the fixture guild."""
    row = {
        "guild_id": fake_ctx.guild_id,
        "iso_week": iso_week,
        "median_income": median,
        "p90_income": cols.get("p90_income", median * 2),
        "active_members": cols.get("active_members", 10),
        "earners": cols.get("earners", 6),
        "minted": cols.get("minted", 1000),
        "burned": cols.get("burned", 400),
        "faucet_mix": cols.get("faucet_mix", '{"logins": 0.5, "activity": 0.3, '
        '"quests": 0.1, "games": 0.1, "grants": 0.0}'),
        "rental_holders": cols.get("rental_holders", 3),
        "rentals_live": cols.get("rentals_live", 4),
        "rentals_ended": cols.get("rentals_ended", 1),
        "streaks_7plus": cols.get("streaks_7plus", 5),
        "grace_used": cols.get("grace_used", 2),
        "computed_at": cols.get("computed_at", 1_700_000_000.0),
    }
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            f"INSERT INTO econ_metrics_weekly ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        conn.commit()


def _non_admin_client(fake_ctx) -> TestClient:
    """A session that is a guild member but holds no admin bit."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth), raise_server_exceptions=False)
    cookie = auth.create_session_cookie(
        user_id=42,
        username="rando",
        access_token="token",
        permission_bits=0,  # no admin / moderator / manage_server
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    return client


# ── GET /api/economy/config ────────────────────────────────────────────


def test_get_economy_config_returns_defaults(authed_client):
    resp = authed_client.get("/api/economy/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["currency_name"] == "Coin"
    assert data["currency_plural"] == "Coins"
    assert data["booster_multiplier"] == 1.5
    assert data["xp_per_coin"] == 15.0
    # Snowflakes leave as strings (see test_get_config_emits_snowflakes_as_
    # strings) — including the 0 sentinel, so the picker always sees one type.
    assert data["bank_channel_id"] == "0"
    assert data["price_role_color"] == 50
    # The QOTD panel reads this straight off the GET — absent means the picker
    # silently renders undefined.
    assert data["qotd_ping_role_id"] == "0"


def test_get_economy_config_requires_admin(fake_ctx):
    client = _non_admin_client(fake_ctx)
    resp = client.get("/api/economy/config")
    assert resp.status_code == 403
    client.close()


# ── PUT /api/economy/config ────────────────────────────────────────────


def test_put_partial_update_roundtrips(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/economy/config",
        json={
            "enabled": True,
            "currency_name": "Gem",
            "currency_plural": "Gems",
            "booster_multiplier": 2.0,
            "bank_channel_id": 555,
            "manager_role_id": 777,
            "reward_qotd": 25,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    with open_db(fake_ctx.db_path) as conn:
        cfg = load_econ_settings(conn, fake_ctx.guild_id)
    assert cfg.enabled is True
    assert cfg.currency_name == "Gem"
    assert cfg.currency_plural == "Gems"
    assert cfg.booster_multiplier == 2.0
    assert cfg.bank_channel_id == 555
    assert cfg.manager_role_id == 777
    assert cfg.reward_qotd == 25
    # Untouched fields keep their defaults.
    assert cfg.wallet_name == "Wallet"


def test_put_streak_shield_price_roundtrips(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/economy/config", json={"price_streak_shield": 45}
    )
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        cfg = load_econ_settings(conn, fake_ctx.guild_id)
    assert cfg.price_streak_shield == 45


def test_put_partial_leaves_other_fields_unset(authed_client, fake_ctx):
    """Only the sent key is written — empty icon URL is settable, and the rest
    of the settings are not persisted."""
    resp = authed_client.put(
        "/api/economy/config", json={"currency_icon_url": ""}
    )
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value

        # currency_name was never written, so no econ_ row exists for it.
        assert get_config_value(
            conn, "econ_currency_name", "SENTINEL", fake_ctx.guild_id
        ) == "SENTINEL"


def test_put_qotd_ping_role_roundtrips(authed_client, fake_ctx):
    """The QOTD page's only knob — settable, and clearable back to no ping."""
    resp = authed_client.put("/api/economy/config", json={"qotd_ping_role_id": 4242})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        assert load_econ_settings(conn, fake_ctx.guild_id).qotd_ping_role_id == 4242

    resp = authed_client.put("/api/economy/config", json={"qotd_ping_role_id": 0})
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        assert load_econ_settings(conn, fake_ctx.guild_id).qotd_ping_role_id == 0


def test_put_rejects_negative_number(authed_client):
    resp = authed_client.put("/api/economy/config", json={"reward_qotd": -1})
    assert resp.status_code == 422


def test_put_rejects_overlong_string(authed_client):
    resp = authed_client.put(
        "/api/economy/config", json={"currency_name": "x" * 33}
    )
    assert resp.status_code == 422


def test_put_rejects_unknown_field(authed_client):
    resp = authed_client.put(
        "/api/economy/config", json={"nonsense_field": 1}
    )
    assert resp.status_code == 422


def test_put_rejects_booster_multiplier_below_one(authed_client):
    resp = authed_client.put(
        "/api/economy/config", json={"booster_multiplier": 0.5}
    )
    assert resp.status_code == 422


def test_put_requires_admin(fake_ctx):
    client = _non_admin_client(fake_ctx)
    resp = client.put("/api/economy/config", json={"enabled": True})
    assert resp.status_code == 403
    client.close()


# ── GET /api/economy/metrics ───────────────────────────────────────────


def test_metrics_empty_state(authed_client):
    """No rollups yet → empty weeks, no hints, zero median."""
    resp = authed_client.get("/api/economy/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["weeks"] == []
    assert data["hints"] == {}
    assert data["median_income"] == 0.0


def test_metrics_returns_weeks_newest_first(authed_client, fake_ctx):
    """Two seeded weeks come back newest-first with faucet_mix left a string."""
    _seed_rollup(fake_ctx, "2026-W01", median=40.0, minted=500, burned=300)
    _seed_rollup(fake_ctx, "2026-W02", median=100.0, minted=1200, burned=400)

    resp = authed_client.get("/api/economy/metrics")
    assert resp.status_code == 200
    data = resp.json()

    weeks = data["weeks"]
    assert len(weeks) == 2
    # Newest ISO week first.
    assert weeks[0]["iso_week"] == "2026-W02"
    assert weeks[1]["iso_week"] == "2026-W01"
    assert weeks[0]["median_income"] == 100.0
    assert weeks[0]["minted"] == 1200
    assert weeks[0]["burned"] == 400
    # faucet_mix stays a JSON string (the tile parses it client-side).
    assert isinstance(weeks[0]["faucet_mix"], str)


def test_metrics_hints_from_latest_week_only(authed_client, fake_ctx):
    """Hints derive from the latest week's median (100), not the older (40)."""
    _seed_rollup(fake_ctx, "2026-W01", median=40.0)
    _seed_rollup(fake_ctx, "2026-W02", median=100.0)

    resp = authed_client.get("/api/economy/metrics")
    data = resp.json()

    assert data["median_income"] == 100.0
    hints = data["hints"]
    # Factors anchored to the spec defaults at median 100.
    assert hints["price_role_color"] == 50
    assert hints["price_role_name"] == 35
    assert hints["price_role_icon"] == 75
    assert hints["price_role_gradient"] == 120
    assert hints["price_text_room"] == 200
    assert hints["price_voice_room"] == 200


def test_metrics_requires_admin(fake_ctx):
    client = _non_admin_client(fake_ctx)
    resp = client.get("/api/economy/metrics")
    assert resp.status_code == 403
    client.close()


# ── snowflake precision (ids above 2**53) ────────────────────────────────────

# A real snowflake from the guild that hit this bug. As a JS number it becomes
# 1526051848518373600 — parseInt and JSON.parse both round it — which is how
# econ_game_role_id came to point at a role that never existed.
BIG_ROLE_ID = 1526051848518373608
BIG_CHANNEL_ID = 1526017396094144584


def test_get_config_emits_snowflakes_as_strings(authed_client, fake_ctx):
    """A bare JSON number would be rounded by the browser's JSON.parse before
    any panel code could defend against it, so ids must leave as strings."""
    with open_db(fake_ctx.db_path) as conn:
        from bot_modules.services.economy_service import save_econ_settings

        save_econ_settings(
            conn,
            fake_ctx.guild_id,
            {"game_role_id": BIG_ROLE_ID, "bank_channel_id": BIG_CHANNEL_ID},
        )

    data = authed_client.get("/api/economy/config").json()

    assert data["game_role_id"] == str(BIG_ROLE_ID)
    assert data["bank_channel_id"] == str(BIG_CHANNEL_ID)
    # No precision lost: the exact digits survive.
    assert int(data["game_role_id"]) == BIG_ROLE_ID
    # Non-id numerics stay numbers — only snowflakes are stringified.
    assert data["reward_qotd"] == 10
    assert data["booster_multiplier"] == 1.5


def test_put_accepts_snowflake_as_string_losslessly(authed_client, fake_ctx):
    """The panel sends ids as strings; pydantic must coerce without rounding."""
    resp = authed_client.put(
        "/api/economy/config",
        json={
            "game_role_id": str(BIG_ROLE_ID),
            "manager_role_id": str(BIG_ROLE_ID),
            "bank_channel_id": str(BIG_CHANNEL_ID),
            "qotd_ping_role_id": str(BIG_ROLE_ID),
        },
    )
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        settings = load_econ_settings(conn, fake_ctx.guild_id)
    assert settings.game_role_id == BIG_ROLE_ID
    assert settings.manager_role_id == BIG_ROLE_ID
    assert settings.bank_channel_id == BIG_CHANNEL_ID
    assert settings.qotd_ping_role_id == BIG_ROLE_ID


def test_snowflake_survives_a_get_put_round_trip(authed_client, fake_ctx):
    """The whole loop the panel performs: read the config, save it back
    unchanged, and the ids must be byte-identical. This is the cycle that
    silently corrupted three settings in production."""
    authed_client.put(
        "/api/economy/config", json={"game_role_id": str(BIG_ROLE_ID)}
    )
    fetched = authed_client.get("/api/economy/config").json()

    # Echo it back exactly as the panel would.
    resp = authed_client.put(
        "/api/economy/config", json={"game_role_id": fetched["game_role_id"]}
    )
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        settings = load_econ_settings(conn, fake_ctx.guild_id)
    assert settings.game_role_id == BIG_ROLE_ID


def test_no_panel_parses_a_snowflake_with_parseint():
    """Guard the browser half of the fix.

    There is no Node in this repo, so the panel JS can't be unit-tested; this
    reads the source instead. `parseInt` on a 19-digit id silently rounds it,
    and that is how three settings in production came to point at objects that
    don't exist. Snowflakes must be sent as strings — the server coerces them.
    """
    import re
    from pathlib import Path

    panels = Path("src/web_server/static/js/panels")
    # parseInt(...) applied to anything that looks like an id source.
    offender = re.compile(r"parseInt\([^)]*(?:getValue\(\)|_id)[^)]*\)")
    hits = []
    for path in sorted(panels.glob("*.js")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if offender.search(line):
                hits.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not hits, (
        "snowflake ids must be sent as strings, not parseInt'd:\n"
        + "\n".join(hits)
    )
