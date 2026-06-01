"""Shared fixtures for web route tests."""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot_modules.core.db_utils import open_db
from migrations import apply_migrations_sync
from web_server.auth import DiscordOAuthAuth, OpenAuth, SESSION_COOKIE
from web_server.deps import invalidate_report_cache
from web_server.server import create_app


class FakeCtx:
    """Minimal AppContext substitute for web route tests."""

    def __init__(self, db_path: Path, guild_id: int = 123):
        self.db_path = db_path
        self.guild_id = guild_id
        self.bot = None
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
        self.mod_role_ids: set[int] = set()
        self.admin_role_ids: set[int] = set()
        self._xp_reload_count = 0
        self._guild_config_cache: dict = {}

    def open_db(self):
        return open_db(self.db_path)

    def reload_xp_settings(self) -> None:
        self._xp_reload_count += 1

    def reload_grant_roles(self) -> None:
        pass

    def reload_permission_roles(self) -> None:
        from bot_modules.core.db_utils import get_config_value

        with self.open_db() as conn:
            mod_raw = get_config_value(conn, "mod_role_ids", "", self.guild_id)
            admin_raw = get_config_value(conn, "admin_role_ids", "", self.guild_id)
        self.mod_role_ids = {int(x) for x in mod_raw.split(",") if x.strip().isdigit()}
        self.admin_role_ids = {int(x) for x in admin_raw.split(",") if x.strip().isdigit()}

    def guild_config(self, guild_id: int):
        from bot_modules.core.app_context import GuildConfig

        cfg = self._guild_config_cache.get(guild_id)
        if cfg is None:
            with self.open_db() as conn:
                cfg = GuildConfig.load(
                    conn, guild_id, allow_legacy_fallback=(guild_id == self.guild_id)
                )
            self._guild_config_cache[guild_id] = cfg
        return cfg

    def invalidate_guild_config(self, guild_id: int) -> None:
        self._guild_config_cache.pop(guild_id, None)


@pytest.fixture
def web_db(tmp_path) -> Path:
    """A fresh SQLite database with full schema applied."""
    db_path = tmp_path / "web_test.db"
    apply_migrations_sync(db_path)
    return db_path


@pytest.fixture
def fake_ctx(web_db) -> FakeCtx:
    return FakeCtx(web_db)


@pytest.fixture
def open_client(fake_ctx) -> Generator[TestClient, None, None]:
    """TestClient with no auth (OpenAuth mode)."""
    app = create_app(fake_ctx, auth=OpenAuth())
    client = TestClient(app)
    invalidate_report_cache()
    yield client
    client.close()
    invalidate_report_cache()


@pytest.fixture
def authed_client(fake_ctx) -> Generator[TestClient, None, None]:
    """TestClient with a Discord OAuth session cookie (primary guild)."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app)
    cookie = auth.create_session_cookie(
        user_id=1,
        username="tester",
        access_token="token",
        permission_bits=0x8,
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    invalidate_report_cache()
    yield client
    client.close()
    invalidate_report_cache()
