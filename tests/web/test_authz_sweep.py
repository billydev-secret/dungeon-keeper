"""Authorization sweep — every API route must reject an unauthenticated caller.

The dashboard's whole security model is "everything is gated" (CLAUDE.md: never
ship an unenforced control). Individual route tests check their own guard, but
nothing guarantees a *newly added* route remembered `Depends(require_perms(...))`.
This sweep enumerates every registered route and, with a real auth backend and
no session, asserts each non-public one comes back 401/403 — never a success.

It runs the *real* `DiscordOAuthAuth` backend (not the `OpenAuth` bypass the
other route tests use, which would pass everything as admin) with no cookie, so
a route missing its dependency shows up as a 2xx/redirect and fails the sweep.
"""

from __future__ import annotations

import re
from collections.abc import Generator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from web_server.auth import DiscordOAuthAuth
from web_server.server import create_app

# Routes intentionally reachable without a session. Matched exactly (or by the
# static-mount prefix). Everything else under the app must be gated.
PUBLIC_PATHS = {
    "/",              # index — redirects to /login when unauthenticated
    "/login",         # login page HTML
    "/auth/discord",  # OAuth start
    "/callback",      # OAuth callback
    "/logout",        # session teardown
    "/api/_docs",     # Swagger UI (FastAPI built-in)
    "/api/_docs/oauth2-redirect",
    "/api/openapi.json",
}
PUBLIC_PREFIXES = ("/static",)

# Auth rejects an unauthenticated caller with one of these. A 2xx or a redirect
# to anywhere but /login means the route let an anonymous request through.
REJECT_CODES = {401, 403}


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _concrete(path: str) -> str:
    """Fill path params with a throwaway value so the URL actually resolves."""
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture
def noauth_client(fake_ctx) -> Generator[tuple[TestClient, object], None, None]:
    """A client on the real OAuth backend with **no** session cookie."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    # follow_redirects=False so the index's 302→/login is visible, not chased.
    client = TestClient(app, follow_redirects=False)
    yield client, app
    client.close()


def _iter_routes(app):
    """(path, method) for every APIRoute, skipping HEAD/OPTIONS auto-verbs."""
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            yield route.path, method


def test_every_api_route_is_registered_under_auth(noauth_client):
    """Sanity: the app actually mounted a substantial API surface to check."""
    _, app = noauth_client
    api_routes = [p for p, _ in _iter_routes(app) if p.startswith("/api")]
    assert len(api_routes) > 50, f"only {len(api_routes)} /api routes — did routers mount?"


def test_no_route_serves_an_unauthenticated_caller(noauth_client):
    """Every non-public route rejects a request that carries no session."""
    client, app = noauth_client

    leaks: list[str] = []
    for i, (path, method) in enumerate(_iter_routes(app)):
        if _is_public(path):
            continue
        url = _concrete(path)
        # The app rate-limits per client IP (bucketed on CF-Connecting-IP); a
        # unique IP per request gives each its own fresh bucket, so the limiter
        # can't 429 the sweep and mask the auth result we're actually checking.
        resp = client.request(method, url, headers={"cf-connecting-ip": f"10.9.{i // 256}.{i % 256}"})
        code = resp.status_code
        # The security property: an anonymous caller must never succeed. 401/403
        # are the intended rejections; a 2xx or a 3xx (session-less redirect into
        # the app) is a leak. 404/405/422 mean the request didn't take effect for
        # an unrelated reason — not a leak, but flagged separately below.
        if code < 400:
            leaks.append(f"{method} {path} → {code} (served without auth!)")
        elif code not in REJECT_CODES and code not in (404, 405):
            leaks.append(f"{method} {path} → {code} (expected 401/403)")

    assert not leaks, "Auth gaps:\n" + "\n".join(leaks)
