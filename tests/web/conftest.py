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

from db_utils import open_db
from migrations import apply_migrations_sync
from web.auth import DiscordOAuthAuth, OpenAuth, SESSION_COOKIE
from web.deps import invalidate_report_cache
from web.server import create_app


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
        self._xp_reload_count = 0

    def open_db(self):
        return open_db(self.db_path)

    def reload_xp_settings(self) -> None:
        self._xp_reload_count += 1

    def reload_grant_roles(self) -> None:
        pass


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
