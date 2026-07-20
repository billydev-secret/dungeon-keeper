"""Snowflake precision sweep — API responses must not leak an int > 2^53.

Discord ids are ~2^60; JavaScript's `Number` loses integer precision above
2^53, so an id that crosses the wire as a bare JSON number is silently rounded
into a *different*, non-existent id on the dashboard (see the "Snowflake JS
precision" note). The fix is to stringify ids in responses — but there's no
central serializer, so every hand-built response is a place to forget.

This does three things:

  * unit-tests the recursive `find_precision_risks` walker;
  * runs it over every no-param GET endpoint with the active guild set to a real
    snowflake, catching any route that echoes the guild id (or any other big
    int) as a number;
  * round-trips snowflake ids through the two hand-serialized features most
    likely to regress (announcements, role menus): create with big ids, GET
    them back, assert they came back as strings.

The GET sweep is broad but not exhaustive — an endpoint returning empty data
passes vacuously. The round-trips cover the concrete serialization paths.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from web_server.auth import OpenAuth
from web_server.deps import invalidate_report_cache
from web_server.server import create_app

# tests/web isn't a package; conftest.py sits beside this file and defines FakeCtx.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import FakeCtx  # noqa: E402

# A real Discord snowflake (the main guild's) — comfortably above 2^53.
SNOWFLAKE = 1469491362444480666
SAFE_MAX = 2**53 - 1

assert SNOWFLAKE > SAFE_MAX  # guard the premise


def find_precision_risks(obj, path: str = "$") -> list[tuple[str, int]]:
    """Every (json-path, value) where an int would lose precision as a JS number.

    Bools are ints in Python but serialize as true/false — skip them. Values
    inside strings are already safe (that's the fix), so only bare ints count.
    """
    risks: list[tuple[str, int]] = []
    if isinstance(obj, bool):
        return risks
    if isinstance(obj, int):
        if abs(obj) > SAFE_MAX:
            risks.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            risks.extend(find_precision_risks(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            risks.extend(find_precision_risks(v, f"{path}[{i}]"))
    return risks


# ── walker unit tests ───────────────────────────────────────────────────────

def test_walker_flags_a_bare_snowflake():
    assert find_precision_risks({"channel_id": SNOWFLAKE}) == [("$.channel_id", SNOWFLAKE)]


def test_walker_passes_a_stringified_snowflake():
    assert find_precision_risks({"channel_id": str(SNOWFLAKE)}) == []


def test_walker_allows_small_and_timestamp_ints():
    # Small ids and ms-timestamps are under 2^53 and cross safely as numbers.
    assert find_precision_risks({"count": 42, "ts": 1_760_000_000_000}) == []


def test_walker_ignores_booleans():
    assert find_precision_risks({"enabled": True, "n": False}) == []


def test_walker_recurses_lists_and_nesting():
    payload = {"items": [{"role_id": SNOWFLAKE}, {"role_id": 5}]}
    assert find_precision_risks(payload) == [("$.items[0].role_id", SNOWFLAKE)]


# ── broad GET sweep with a snowflake active guild ───────────────────────────

@pytest.fixture
def snowflake_client() -> Generator[tuple[TestClient, object], None, None]:
    """OpenAuth client whose active guild id is a real snowflake.

    Under OpenAuth the active guild is ``ctx.guild_id`` (deps.get_active_guild_id),
    so any endpoint echoing the guild id surfaces the snowflake for the walker.
    """
    import tempfile
    from pathlib import Path

    from migrations import apply_migrations_sync

    td = tempfile.mkdtemp()
    db = Path(td) / "snowflake.db"
    apply_migrations_sync(db)
    ctx = FakeCtx(db, guild_id=SNOWFLAKE)
    app = create_app(ctx, auth=OpenAuth())
    # raise_server_exceptions=False: a handler that 500s on an empty DB is another
    # test's problem — the sweep only inspects 200 JSON bodies, so let it see the
    # 500 as a response and skip it rather than re-raising into the test.
    client = TestClient(app, raise_server_exceptions=False)
    invalidate_report_cache()
    yield client, app
    client.close()
    invalidate_report_cache()


# TestClient runs the app in-process, so a slow/blocking handler can't be timed
# out — it just hangs the suite. These prefixes do heavy work or call out to the
# LLM / external services / large scans; they aren't id-echoing config routes, so
# excluding them keeps the sweep fast and hang-free without losing coverage that
# matters here. (Snowflake safety on those routes rides on their own tests.)
_HEAVY_PREFIXES = (
    "/api/reports",
    "/api/health",
    "/api/ai",
    "/api/logs",
    "/api/messages",
    "/api/admin",
    "/api/rules-watch",
    "/api/config/voice-transcription",
)


def _noparam_get_paths(app) -> list[str]:
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods and "{" not in route.path:
            if route.path.startswith("/api") and not route.path.startswith(_HEAVY_PREFIXES):
                out.append(route.path)
    return sorted(set(out))


def test_no_get_endpoint_echoes_a_bare_snowflake(snowflake_client):
    """Hit every no-param GET /api route; no 200 JSON body may carry a big int."""
    client, app = snowflake_client
    paths = _noparam_get_paths(app)
    assert len(paths) > 15, f"only {len(paths)} sweepable GET routes — routers mounted?"

    risks: list[str] = []
    for i, path in enumerate(paths):
        resp = client.get(path, headers={"cf-connecting-ip": f"10.8.{i // 256}.{i % 256}"})
        if resp.status_code != 200:
            continue  # status is another test's concern; only inspect real bodies
        ctype = resp.headers.get("content-type", "")
        if "application/json" not in ctype:
            continue
        for jpath, val in find_precision_risks(resp.json()):
            risks.append(f"GET {path} → {jpath} = {val}")

    assert not risks, "Snowflake precision loss (id returned as a number):\n" + "\n".join(risks)


# ── round-trips through the hand-serialized routes ──────────────────────────

def test_announcement_snowflakes_round_trip_as_strings(open_client):
    """Create an announcement carrying snowflake channel + role-button ids; the
    listing must return them as strings, not numbers."""
    body = {
        "channel_id": str(SNOWFLAKE),
        "title": "Snowflake test",
        "body": "x",
        "buttons": [{"role_id": str(SNOWFLAKE + 1), "label": "Get role", "style": "primary"}],
    }
    resp = open_client.post("/api/announcements", json=body)
    assert resp.status_code == 200, resp.text

    listing = open_client.get("/api/announcements").json()
    assert not find_precision_risks(listing), find_precision_risks(listing)
    item = listing["items"][0]
    assert item["channel_id"] == str(SNOWFLAKE)
    assert item["buttons"][0]["role_id"] == str(SNOWFLAKE + 1)


def test_role_menu_snowflakes_round_trip_as_strings(open_client, fake_ctx):
    """A role menu with a snowflake role option must read back with the id as a
    string. Seeded straight into the DB — the save path needs a live bot to
    validate roles, but the serialization we're checking is on the read path."""
    import time

    from bot_modules.role_menus import db as menus_db

    with fake_ctx.open_db() as conn:
        menu_id = menus_db.create_menu(conn, fake_ctx.guild_id, "Colors", 1, time.time())
        menus_db.replace_options(
            conn, menu_id, [{"role_id": SNOWFLAKE, "label": "Red"}], time.time()
        )

    menu = open_client.get(f"/api/role-menus/{menu_id}").json()
    assert not find_precision_risks(menu), find_precision_risks(menu)
    assert menu["options"][0]["role_id"] == str(SNOWFLAKE)
