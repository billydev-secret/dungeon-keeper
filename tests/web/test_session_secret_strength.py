"""SESSION_SECRET strength gate (GDPR Art 32 / 2026-08 review).

A forged session cookie is a full admin session — the cookie carries the
permission bits the cache-miss fallback trusts — and the cloudflared tunnel
publishes this dashboard. So a weak secret fails the boot, matching how
_auto_detect_auth already treats a *missing* secret.
"""

from __future__ import annotations

import pytest

from web_server.server import _require_strong_session_secret


def test_accepts_the_documented_recipe():
    """DEPLOYMENT.md says secrets.token_urlsafe(32), which is 43 chars."""
    import secrets

    _require_strong_session_secret(secrets.token_urlsafe(32))


def test_accepts_the_live_length_floor():
    _require_strong_session_secret("a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6")  # 32


@pytest.mark.parametrize(
    "secret, expected",
    [
        pytest.param("short", "minimum 32", id="too-short"),
        pytest.param("", "minimum 32", id="empty"),
        pytest.param("a" * 64, "distinct characters", id="long-but-degenerate"),
        pytest.param("ababab" * 12, "distinct characters", id="low-alphabet"),
    ],
)
def test_rejects_weak_secrets(secret, expected):
    with pytest.raises(RuntimeError) as exc:
        _require_strong_session_secret(secret)
    assert expected in str(exc.value)
    assert "token_urlsafe" in str(exc.value)  # tells the operator the fix
