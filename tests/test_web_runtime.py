from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request

from web.auth import OpenAuth
from web.deps import invalidate_report_cache, store_report_result
from web.routes import oauth as oauth_routes
from web.wellness_routes.deps import get_current_user


def _make_request(auth):
    app = SimpleNamespace(
        state=SimpleNamespace(
            auth=auth,
            ctx=SimpleNamespace(guild_id=123, bot=None),
        )
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/wellness/me",
        "query_string": b"",
        "headers": [],
        "app": app,
    }
    return Request(scope)


# ── Wellness auth ─────────────────────────────────────────────────────

async def test_open_auth_is_accepted_by_wellness_deps():
    request = _make_request(OpenAuth())
    user = await get_current_user(request=request, auth=OpenAuth())
    assert user is not None
    assert user.username == "anonymous"
    assert "manage_server" in user.perms


# ── Report cache ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_report_cache():
    invalidate_report_cache()
    yield
    invalidate_report_cache()


def test_invalidate_report_cache_scopes_to_matching_entries():
    store_report_result("role-growth", 1, {"resolution": "week"}, {"ok": 1})
    store_report_result("role-growth", 2, {"resolution": "week"}, {"ok": 2})
    store_report_result("message-rate", 1, {"days": 30}, {"ok": 3})

    removed = invalidate_report_cache(guild_id=1)

    assert removed == 2
    assert invalidate_report_cache() == 1


# ── OAuth routes ──────────────────────────────────────────────────────

def test_base_url_strips_callback_suffix():
    with patch.dict(os.environ, {"DASHBOARD_BASE_URL": "https://example.com/callback"}, clear=False):
        assert oauth_routes._base_url() == "https://example.com"


def test_return_to_allows_same_host_and_blocks_external():
    with patch.dict(
        os.environ,
        {"DASHBOARD_BASE_URL": "https://example.com", "DASHBOARD_RETURN_TO_URLS": ""},
        clear=False,
    ):
        assert oauth_routes._is_safe_return_to("/wellness") is True
        assert oauth_routes._is_safe_return_to("https://example.com:8079/wellness") is True
        assert oauth_routes._is_safe_return_to("https://evil.example/wellness") is False
