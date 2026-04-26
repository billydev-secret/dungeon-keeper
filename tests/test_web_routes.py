from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from db_utils import (
    add_config_id,
    add_grant_permission,
    get_config_id_set,
    get_config_value,
    get_grant_permissions,
    get_grant_roles,
    init_config_db,
    init_grant_role_tables,
    open_db,
    set_config_value,
    upsert_grant_role,
)
from services.auto_delete_service import init_auto_delete_tables, upsert_auto_delete_rule
from services.booster_roles import init_booster_role_tables, upsert_booster_role
from services.confessions_service import init_db as init_confessions_db
from services.inactivity_prune_service import init_inactivity_prune_tables
from services.message_store import (
    init_known_channels_table,
    init_known_users_table,
    upsert_known_channel,
    upsert_known_user,
)
from web.auth import DiscordOAuthAuth, OpenAuth, SESSION_COOKIE
from web.deps import invalidate_report_cache, store_report_result
from web.server import create_app


class _TestCtx:
    def __init__(self, db_path: Path, guild_id: int = 123):
        self.db_path = db_path
        self.guild_id = guild_id
        self.bot: Any = None
        self.tz_offset_hours = 0.0
        self.mod_channel_id = 0
        self.bypass_role_ids: set[int] = set()
        self.recorded_bot_user_ids: set[int] = set()
        self.spoiler_required_channels: set[int] = set()
        self.level_5_role_id = 0
        self.level_5_log_channel_id = 0
        self.level_up_log_channel_id = 0
        self.xp_grant_allowed_user_ids: set[int] = set()
        self.xp_excluded_channel_ids: set[int] = set()
        self.welcome_channel_id = 0
        self.welcome_message = ""
        self.welcome_ping_role_id = 0
        self.leave_channel_id = 0
        self.leave_message = ""
        self.greeter_role_id = 0
        self.greeter_chat_channel_id = 0
        self.join_leave_log_channel_id = 0
        self.reload_xp_settings_calls = 0

    def open_db(self):
        return open_db(self.db_path)

    def reload_xp_settings(self) -> None:
        self.reload_xp_settings_calls += 1

    def reload_grant_roles(self) -> None:
        pass


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def ctx(tmp_path):
    db_path = tmp_path / "test.db"
    init_config_db(db_path)
    with open_db(db_path) as conn:
        init_grant_role_tables(conn)
        init_inactivity_prune_tables(conn)
        init_known_users_table(conn)
        init_known_channels_table(conn)
        init_booster_role_tables(conn)
        init_auto_delete_tables(conn)
    init_confessions_db(db_path)
    return _TestCtx(db_path)


@pytest.fixture(autouse=True)
def clear_cache():
    invalidate_report_cache()
    yield
    invalidate_report_cache()


@pytest.fixture
def make_client(ctx):
    clients = []

    def _make(*, auth_mode: str = "open", active_guild_id: int | None = None, permission_bits: int = 0x8) -> TestClient:
        if auth_mode == "open":
            auth = OpenAuth()
        else:
            auth = DiscordOAuthAuth("test-secret", ctx.guild_id)

        app = create_app(ctx, auth=auth)
        client = TestClient(app)
        clients.append(client)

        if auth_mode == "discord":
            guild_id = active_guild_id if active_guild_id is not None else ctx.guild_id
            cookie = auth.create_session_cookie(  # type: ignore[union-attr]
                user_id=1,
                username="tester",
                access_token="token",
                permission_bits=permission_bits,
                guild_id=guild_id,
                guilds=[{"id": guild_id, "name": f"Guild {guild_id}", "icon": None}],
            )
            client.cookies.set(SESSION_COOKIE, cookie)

        return client

    yield _make

    for c in clients:
        c.close()


# ── Tests ─────────────────────────────────────────────────────────────


def test_get_config_returns_active_guild_scoped_values(ctx, make_client):
    secondary_guild_id = 999
    with open_db(ctx.db_path) as conn:
        set_config_value(conn, "guild_id", str(ctx.guild_id), ctx.guild_id)
        set_config_value(conn, "mod_channel_id", "111", ctx.guild_id)
        set_config_value(conn, "welcome_message", "Primary welcome", ctx.guild_id)

        set_config_value(conn, "guild_id", str(secondary_guild_id), secondary_guild_id)
        set_config_value(conn, "tz_offset_hours", "5.5", secondary_guild_id)
        set_config_value(conn, "mod_channel_id", "222", secondary_guild_id)
        set_config_value(conn, "booster_swatch_dir", "C:/secondary/swatches", secondary_guild_id)
        set_config_value(conn, "welcome_channel_id", "333", secondary_guild_id)
        set_config_value(conn, "welcome_message", "Secondary welcome", secondary_guild_id)
        set_config_value(conn, "join_leave_log_channel_id", "334", secondary_guild_id)
        set_config_value(conn, "xp_level_5_role_id", "444", secondary_guild_id)
        set_config_value(conn, "warning_threshold", "7", secondary_guild_id)
        add_config_id(conn, "bypass_role_ids", 42, secondary_guild_id)
        add_config_id(conn, "recorded_bot_user_ids", 77, secondary_guild_id)
        add_config_id(conn, "xp_grant_allowed_user_ids", 88, secondary_guild_id)
        add_config_id(conn, "xp_excluded_channel_ids", 99, secondary_guild_id)
        add_config_id(conn, "spoiler_required_channels", 100, secondary_guild_id)
        conn.execute(
            "INSERT INTO inactivity_prune_rules (guild_id, role_id, inactivity_days) VALUES (?, ?, ?)",
            (secondary_guild_id, 555, 30),
        )
        conn.execute(
            "INSERT INTO inactivity_prune_exceptions (guild_id, user_id) VALUES (?, ?)",
            (secondary_guild_id, 888),
        )
        upsert_known_user(
            conn,
            guild_id=secondary_guild_id,
            user_id=888,
            username="fallback-user",
            display_name="Fallback User",
            ts=1.0,
        )
        upsert_grant_role(
            conn,
            secondary_guild_id,
            "denizen",
            label="Denizen",
            role_id=901,
            log_channel_id=902,
            announce_channel_id=903,
            grant_message="Granted!",
        )
        add_grant_permission(conn, secondary_guild_id, "denizen", "user", 777)
        upsert_booster_role(
            conn,
            secondary_guild_id,
            "rose",
            label="Rose",
            role_id=987,
            image_path="C:/rose.png",
            sort_order=1,
        )
    upsert_auto_delete_rule(ctx.db_path, secondary_guild_id, 600, 3600, 7200)

    client = make_client(auth_mode="discord", active_guild_id=secondary_guild_id)
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert data["global"]["guild_id"] == secondary_guild_id
    assert data["global"]["tz_offset_hours"] == 5.5
    assert data["global"]["mod_channel_id"] == "222"
    assert data["global"]["bypass_role_ids"] == ["42"]
    assert data["global"]["recorded_bot_user_ids"] == ["77"]
    assert data["global"]["booster_swatch_dir"] == "C:/secondary/swatches"
    assert data["welcome"]["welcome_channel_id"] == "333"
    assert data["welcome"]["welcome_message"] == "Secondary welcome"
    assert data["welcome"]["join_leave_log_channel_id"] == "334"
    assert data["xp"]["level_5_role_id"] == "444"
    assert data["xp"]["xp_grant_allowed_user_ids"] == ["88"]
    assert data["xp"]["xp_excluded_channel_ids"] == ["99"]
    assert data["spoiler"]["spoiler_required_channels"] == ["100"]
    assert data["prune"]["role_id"] == "555"
    assert data["prune"]["inactivity_days"] == 30
    assert data["prune"]["exemptions"] == [{"id": "888", "name": "Fallback User"}]
    assert data["moderation"]["warning_threshold"] == 7
    assert data["roles"]["denizen"]["role_id"] == "901"
    assert data["roles"]["denizen"]["permissions"] == [{"entity_type": "user", "entity_id": "777"}]
    assert data["booster_roles"][0]["role_key"] == "rose"
    assert data["auto_delete"][0]["channel_id"] == "600"


def test_update_global_persists_fields_and_updates_context(ctx, make_client):
    client = make_client()

    response = client.put(
        "/api/config/global",
        json={
            "tz_offset_hours": -4.5,
            "mod_channel_id": "111",
            "bypass_role_ids": ["2", "1"],
            "recorded_bot_user_ids": ["9"],
            "booster_swatch_dir": "C:/swatches",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    with open_db(ctx.db_path) as conn:
        assert get_config_value(conn, "tz_offset_hours", "", ctx.guild_id) == "-4.5"
        assert get_config_value(conn, "mod_channel_id", "", ctx.guild_id) == "111"
        assert get_config_value(conn, "booster_swatch_dir", "", ctx.guild_id) == "C:/swatches"
        assert get_config_id_set(conn, "bypass_role_ids", ctx.guild_id) == {1, 2}
        assert get_config_id_set(conn, "recorded_bot_user_ids", ctx.guild_id) == {9}
    assert ctx.tz_offset_hours == -4.5
    assert ctx.mod_channel_id == 111
    assert ctx.bypass_role_ids == {1, 2}
    assert ctx.recorded_bot_user_ids == {9}


def test_update_welcome_persists_and_updates_live_context(ctx, make_client):
    client = make_client()

    response = client.put(
        "/api/config/welcome",
        json={
            "welcome_channel_id": "500",
            "welcome_message": "Hello there",
            "welcome_ping_role_id": "501",
            "leave_channel_id": "502",
            "leave_message": "Goodbye",
            "greeter_role_id": "503",
            "greeter_chat_channel_id": "504",
            "join_leave_log_channel_id": "505",
        },
    )

    assert response.status_code == 200
    with open_db(ctx.db_path) as conn:
        assert get_config_value(conn, "welcome_message", "", ctx.guild_id) == "Hello there"
        assert get_config_value(conn, "greeter_role_id", "", ctx.guild_id) == "503"
        assert get_config_value(conn, "greeter_chat_channel_id", "", ctx.guild_id) == "504"
        assert get_config_value(conn, "join_leave_log_channel_id", "", ctx.guild_id) == "505"
    assert ctx.welcome_channel_id == 500
    assert ctx.welcome_message == "Hello there"
    assert ctx.welcome_ping_role_id == 501
    assert ctx.leave_channel_id == 502
    assert ctx.leave_message == "Goodbye"
    assert ctx.greeter_role_id == 503
    assert ctx.greeter_chat_channel_id == 504
    assert ctx.join_leave_log_channel_id == 505


def test_update_xp_persists_coefficients_and_reloads_context(ctx, make_client):
    client = make_client()

    response = client.put(
        "/api/config/xp",
        json={
            "level_5_role_id": "10",
            "level_5_log_channel_id": "11",
            "level_up_log_channel_id": "12",
            "xp_grant_allowed_user_ids": ["13", "14"],
            "xp_excluded_channel_ids": ["15"],
            "message_word_xp": 1.5,
            "cooldown_thresholds_seconds": "10,20",
            "voice_interval_seconds": 600,
        },
    )

    assert response.status_code == 200
    with open_db(ctx.db_path) as conn:
        assert get_config_value(conn, "xp_level_5_role_id", "", ctx.guild_id) == "10"
        assert get_config_value(conn, "xp_level_5_log_channel_id", "", ctx.guild_id) == "11"
        assert get_config_value(conn, "xp_level_up_log_channel_id", "", ctx.guild_id) == "12"
        assert get_config_id_set(conn, "xp_grant_allowed_user_ids", ctx.guild_id) == {13, 14}
        assert get_config_id_set(conn, "xp_excluded_channel_ids", ctx.guild_id) == {15}
        assert get_config_value(conn, "xp_coeff_message_word_xp", "", ctx.guild_id) == "1.5"
        assert get_config_value(conn, "xp_coeff_cooldown_thresholds_seconds", "", ctx.guild_id) == "10,20"
        assert get_config_value(conn, "xp_coeff_voice_interval_seconds", "", ctx.guild_id) == "600"
    assert ctx.level_5_role_id == 10
    assert ctx.level_5_log_channel_id == 11
    assert ctx.level_up_log_channel_id == 12
    assert ctx.xp_grant_allowed_user_ids == {13, 14}
    assert ctx.xp_excluded_channel_ids == {15}
    assert ctx.reload_xp_settings_calls == 1


def test_update_role_grant_creates_row_and_permissions(ctx, make_client):
    client = make_client()

    response = client.put(
        "/api/config/roles/nightwatch",
        json={
            "label": "Night Watch",
            "role_id": "701",
            "log_channel_id": "702",
            "announce_channel_id": "703",
            "grant_message": "Stay sharp.",
            "permissions": [
                {"entity_type": "role", "entity_id": "704"},
                {"entity_type": "user", "entity_id": "705"},
            ],
        },
    )

    assert response.status_code == 200
    with open_db(ctx.db_path) as conn:
        grant = get_grant_roles(conn, ctx.guild_id)["nightwatch"]
        permissions = set(get_grant_permissions(conn, ctx.guild_id, "nightwatch"))
    assert grant["label"] == "Night Watch"
    assert grant["role_id"] == 701
    assert grant["log_channel_id"] == 702
    assert grant["announce_channel_id"] == 703
    assert grant["grant_message"] == "Stay sharp."
    assert permissions == {("role", 704), ("user", 705)}


def test_config_edits_require_primary_guild(ctx, make_client):
    client = make_client(auth_mode="discord", active_guild_id=999)

    response = client.put("/api/config/global", json={"tz_offset_hours": 3.0})

    assert response.status_code == 403
    assert "primary guild" in response.json()["detail"].lower()


def test_reports_cache_clear_endpoint_scopes_to_active_guild(ctx, make_client):
    store_report_result("role-growth", 999, {"resolution": "week"}, {"ok": 1})
    store_report_result("message-rate", 999, {"days": 30}, {"ok": 2})
    store_report_result("role-growth", 123, {"resolution": "week"}, {"ok": 3})
    client = make_client(auth_mode="discord", active_guild_id=999)

    response = client.post("/api/reports/cache/clear")

    assert response.status_code == 200
    assert response.json() == {"cleared": 2}
    assert invalidate_report_cache() == 1


def test_role_growth_endpoint_caches_results_and_parses_roles(ctx, make_client):
    ctx.tz_offset_hours = 3.5
    client = make_client()
    payload = {
        "resolution": "month",
        "window_label": "Last 3 Months",
        "labels": ["Jan", "Feb"],
        "series": [{"role": "alpha", "counts": [1, 2]}],
    }

    with patch(
        "web.routes.reports.reports_data.get_role_growth_data",
        return_value=payload,
    ) as mock_role_growth:
        first = client.get("/api/reports/role-growth?resolution=month&roles=alpha, beta,,")
        second = client.get("/api/reports/role-growth?resolution=month&roles=alpha, beta,,")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == payload
    assert second.json() == payload
    assert mock_role_growth.call_count == 1
    args, kwargs = mock_role_growth.call_args
    assert args[1] == ctx.guild_id
    assert args[2] == "month"
    assert args[3] == {"alpha", "beta"}
    assert kwargs["utc_offset_hours"] == 3.5


def test_message_rate_clamps_days_before_query(ctx, make_client):
    client = make_client()
    payload = {
        "days": 365,
        "tz_label": "UTC",
        "buckets": list(range(7)),
        "avg_per_day": [1.0] * 7,
    }

    with patch(
        "web.routes.reports.reports_data.get_message_rate_data",
        return_value=payload,
    ) as mock_message_rate:
        response = client.get("/api/reports/message-rate?days=9999")

    assert response.status_code == 200
    assert response.json() == payload
    args, _kwargs = mock_message_rate.call_args
    assert args[1] == ctx.guild_id
    assert args[2] == 365
    assert args[3] == 0.0


def test_channel_comparison_resolves_names_from_guild_and_db(ctx, make_client):
    with open_db(ctx.db_path) as conn:
        upsert_known_channel(
            conn,
            guild_id=ctx.guild_id,
            channel_id=222,
            channel_name="archived-channel",
            ts=1.0,
        )

    live_channel = SimpleNamespace(id=111, name="live-channel")
    guild = SimpleNamespace(
        get_channel=lambda channel_id: live_channel if channel_id == 111 else None
    )
    ctx.bot = SimpleNamespace(
        get_guild=lambda guild_id: guild if guild_id == ctx.guild_id else None
    )
    client = make_client()
    payload = {
        "channels": [
            {
                "channel_id": "111",
                "channel_name": "",
                "message_count": 50,
                "unique_authors": 10,
                "recent_count": 30,
                "prev_count": 20,
                "trend_pct": 50.0,
                "total_xp": 0.0,
                "gini": 0.0,
                "avg_sentiment": None,
            },
            {
                "channel_id": "222",
                "channel_name": "",
                "message_count": 15,
                "unique_authors": 5,
                "recent_count": 8,
                "prev_count": 7,
                "trend_pct": 14.3,
                "total_xp": 0.0,
                "gini": 0.0,
                "avg_sentiment": None,
            },
        ]
    }

    with patch(
        "web.routes.reports.reports_data.get_channel_comparison_data",
        return_value=payload,
    ):
        response = client.get("/api/reports/channel-comparison?days=7")

    assert response.status_code == 200
    data = response.json()
    assert data["channels"][0]["channel_name"] == "live-channel"
    assert data["channels"][1]["channel_name"] == "archived-channel"


def _mk_channel(spec_cls, *, ch_id: int, name: str, category=None, nsfw: bool = False):
    """Build a MagicMock that passes isinstance(ch, spec_cls)."""
    import discord
    from unittest.mock import MagicMock

    ch = MagicMock(spec=spec_cls)
    ch.id = ch_id
    ch.name = name
    ch.category = category
    if spec_cls is not discord.CategoryChannel:
        ch.nsfw = nsfw
    return ch


def test_meta_channels_default_returns_text_and_thread_only(ctx, make_client):
    import discord

    category = _mk_channel(discord.CategoryChannel, ch_id=10, name="General")
    text_ch = _mk_channel(discord.TextChannel, ch_id=11, name="general", category=category)
    voice_ch = _mk_channel(discord.VoiceChannel, ch_id=12, name="Lobby", category=category)
    thread_ch = _mk_channel(discord.Thread, ch_id=13, name="thread-a", category=None)

    guild = SimpleNamespace(channels=[category, text_ch, voice_ch, thread_ch])
    ctx.bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == ctx.guild_id else None
    )
    client = make_client()

    response = client.get("/api/meta/channels")
    assert response.status_code == 200
    data = response.json()
    types_returned = sorted(c["type"] for c in data)
    assert types_returned == ["text", "thread"]


def test_meta_channels_includes_voice_and_category_when_requested(ctx, make_client):
    import discord

    category = _mk_channel(discord.CategoryChannel, ch_id=10, name="General")
    text_ch = _mk_channel(discord.TextChannel, ch_id=11, name="general", category=category)
    voice_ch = _mk_channel(discord.VoiceChannel, ch_id=12, name="Lobby", category=category)

    guild = SimpleNamespace(channels=[category, text_ch, voice_ch])
    ctx.bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == ctx.guild_id else None
    )
    client = make_client()

    response = client.get("/api/meta/channels?types=text,voice,category")
    assert response.status_code == 200
    data = response.json()
    by_id = {c["id"]: c for c in data}
    assert by_id["10"]["type"] == "category"
    assert by_id["11"]["type"] == "text"
    assert by_id["12"]["type"] == "voice"
    assert by_id["12"]["name"] == "Lobby"
