"""Regression tests for the 2026-08-06 website deep review security findings.

One file per review batch rather than per route, because the findings cut
across auth, three route modules and the wellness API — and each test is
pinned to the finding id it defends so a future reader can trace it back.

Covered here: B-SEC1 (cross-guild permission carry-over), B-SEC2/B-PERF2
(regex search rails), B-SEC3 (moderator→admin via role menus), B-SEC4 (OAuth
token in the cookie), B-SEC5 (server-side logout), B-SEC6 (reflected XSS in
the Spotify callback), B-SEC7 (top-channels visibility), B-SEC8 (snowflake
precision) and B-SEC10 (Origin/Referer CSRF backstop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db
from bot_modules.services.wellness_service import add_cap, opt_in_user
from web_server.auth import SESSION_COOKIE, DiscordOAuthAuth
from web_server.server import create_app

_ADMIN_BITS = 0x8
_MOD_BITS = 0x2000  # MANAGE_MESSAGES — moderator, decidedly not admin
_OTHER_GUILD = 456


# ── Session helpers ──────────────────────────────────────────────────


def _auth_client(fake_ctx, *, bits: int, guild_id: int | None = None, guilds=None):
    """A TestClient whose cookie carries ``bits`` for ``guild_id``."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app)
    active = guild_id or fake_ctx.guild_id
    cookie = auth.create_session_cookie(
        user_id=1,
        username="tester",
        access_token="unused",
        permission_bits=bits,
        guild_id=active,
        guilds=guilds
        or [
            {"id": fake_ctx.guild_id, "name": "Home", "icon": None},
            {"id": _OTHER_GUILD, "name": "Other", "icon": None},
        ],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    return auth, client


# ── B-SEC1 — permissions do not follow the user across a guild switch ─


def test_stale_bits_from_another_guild_grant_nothing(fake_ctx):
    """Session bits captured in guild A must not apply while guild B is active.

    The live gateway cache normally re-resolves permissions per request and
    hides this; here the bot is offline (``fake_ctx.bot is None``), which is
    exactly the startup / kicked-from-guild / standalone window the fallback
    exists for.
    """
    auth, client = _auth_client(fake_ctx, bits=_ADMIN_BITS)

    # Admin in the home guild — the baseline the fallback is meant to preserve.
    assert client.get("/api/system/stats").status_code == 200

    # Forge the pre-fix shape: active guild switched, bits left behind.
    stale = auth._serializer.dumps(
        {
            "uid": 1,
            "name": "tester",
            "perms_bits": _ADMIN_BITS,
            "perms_guild_id": fake_ctx.guild_id,
            "role_ids": [7],
            "role_names": ["Admins"],
            "guild_id": _OTHER_GUILD,
            "guilds": [
                {"id": fake_ctx.guild_id, "name": "Home", "icon": None},
                {"id": _OTHER_GUILD, "name": "Other", "icon": None},
            ],
            "avatar_url": None,
            "gen": 0,
        }
    )
    client.cookies.set(SESSION_COOKIE, stale)
    assert client.get("/api/system/stats").status_code == 403

    me = client.get("/api/me").json()
    assert me["perms"] == []
    assert me["role_ids"] == [] and me["role_names"] == []
    client.close()


def test_legacy_session_without_perms_guild_id_fails_closed(fake_ctx):
    """A cookie minted before the field existed can't prove which guild it's for."""
    auth, client = _auth_client(fake_ctx, bits=_ADMIN_BITS)
    legacy = auth._serializer.dumps(
        {
            "uid": 1,
            "name": "tester",
            "perms_bits": _ADMIN_BITS,
            "role_ids": [],
            "role_names": [],
            "guild_id": fake_ctx.guild_id,
            "guilds": [{"id": fake_ctx.guild_id, "name": "Home", "icon": None}],
            "avatar_url": None,
        }
    )
    client.cookies.set(SESSION_COOKIE, legacy)
    assert client.get("/api/system/stats").status_code == 403
    client.close()


def test_select_guild_clears_unresolvable_permissions(fake_ctx):
    """Switching guilds while the bot is offline drops the old guild's bits."""
    _auth, client = _auth_client(fake_ctx, bits=_ADMIN_BITS)
    assert client.get("/api/system/stats").status_code == 200

    resp = client.post(f"/api/guilds/{_OTHER_GUILD}/select")
    assert resp.status_code == 200, resp.text
    assert resp.json()["perms"] == []

    # The re-signed cookie the response set must not carry admin forward.
    assert client.get("/api/system/stats").status_code == 403
    client.close()


def test_select_guild_recaptures_permissions_for_the_target(fake_ctx):
    """When the gateway cache has the target guild, bits are re-read there."""
    member = SimpleNamespace(
        guild_permissions=SimpleNamespace(value=_ADMIN_BITS),
        roles=[SimpleNamespace(id=9, name="Boss", is_default=lambda: False)],
        status="online",
        display_name="tester",
    )
    target = SimpleNamespace(
        id=_OTHER_GUILD,
        name="Other",
        get_member=lambda _uid: member,
    )
    fake_ctx.bot = SimpleNamespace(
        get_guild=lambda gid: target if gid == _OTHER_GUILD else None
    )
    _auth, client = _auth_client(fake_ctx, bits=0)

    resp = client.post(f"/api/guilds/{_OTHER_GUILD}/select")
    assert resp.status_code == 200, resp.text
    assert "admin" in resp.json()["perms"]

    # And the stored bits now describe the target guild, so the fallback works.
    fake_ctx.bot = None
    assert client.get("/api/system/stats").status_code == 200
    client.close()


# ── B-SEC4 — the OAuth access token stays out of the cookie ───────────


def test_session_cookie_does_not_store_the_oauth_token(fake_ctx):
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    cookie = auth.create_session_cookie(
        user_id=1, username="t", access_token="super-secret-token"
    )
    session = auth.read_session(cookie)
    assert session is not None
    assert "token" not in session
    assert "super-secret-token" not in cookie


# ── B-SEC5 — logout revokes server-side ──────────────────────────────


def test_logout_invalidates_a_captured_cookie(fake_ctx):
    auth, client = _auth_client(fake_ctx, bits=_ADMIN_BITS)
    captured = client.cookies.get(SESSION_COOKIE)
    assert client.get("/api/system/stats").status_code == 200

    assert client.get("/logout", follow_redirects=False).status_code == 302

    # The attacker still holds the exact cookie bytes; they must no longer work.
    client.cookies.set(SESSION_COOKIE, captured)
    assert client.get("/api/system/stats").status_code == 401
    client.close()


def test_logout_does_not_lock_the_user_out_of_future_logins(fake_ctx):
    """Revocation is a floor, not a ban — a fresh cookie at the new generation works."""
    auth, client = _auth_client(fake_ctx, bits=_ADMIN_BITS)
    client.get("/logout", follow_redirects=False)

    fresh = auth.create_session_cookie(
        user_id=1,
        username="tester",
        access_token="",
        permission_bits=_ADMIN_BITS,
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Home", "icon": None}],
        generation=auth.current_generation(fake_ctx, 1),
    )
    client.cookies.set(SESSION_COOKIE, fresh)
    assert client.get("/api/system/stats").status_code == 200
    client.close()


def test_revocation_survives_a_restart(fake_ctx):
    """The generation lives in the DB, so a new process still refuses the cookie."""
    auth, client = _auth_client(fake_ctx, bits=_ADMIN_BITS)
    captured = client.cookies.get(SESSION_COOKIE)
    client.get("/logout", follow_redirects=False)
    client.close()

    # Fresh backend + app == a restarted dashboard: empty in-memory cache.
    restarted = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    fresh_client = TestClient(create_app(fake_ctx, auth=restarted))
    fresh_client.cookies.set(SESSION_COOKIE, captured)
    assert fresh_client.get("/api/system/stats").status_code == 401
    fresh_client.close()


# ── B-SEC10 — Origin / Referer check on state-changing verbs ─────────


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({}, 200, id="no-origin-header-is-allowed"),
        pytest.param({"Origin": "http://testserver"}, 200, id="same-origin"),
        pytest.param(
            {"Origin": "http://testserver:9999"}, 200, id="same-host-other-port"
        ),
        pytest.param({"Referer": "http://testserver/#/home"}, 200, id="same-referer"),
        pytest.param({"Origin": "https://evil.example"}, 403, id="cross-origin"),
        pytest.param(
            {"Referer": "https://evil.example/page"}, 403, id="cross-origin-referer"
        ),
        pytest.param({"Origin": "null"}, 403, id="opaque-origin"),
    ],
)
def test_origin_check_on_state_changing_requests(fake_ctx, headers, expected):
    _auth, client = _auth_client(fake_ctx, bits=_ADMIN_BITS)
    resp = client.post(f"/api/guilds/{fake_ctx.guild_id}/select", headers=headers)
    assert resp.status_code == expected, resp.text
    client.close()


def test_origin_check_does_not_touch_reads(fake_ctx):
    """A foreign Origin on a GET is harmless — SameSite still guards the cookie."""
    _auth, client = _auth_client(fake_ctx, bits=_ADMIN_BITS)
    resp = client.get("/api/system/stats", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
    client.close()


# ── B-SEC3 — role menus are not a moderator→admin escalation ─────────


@dataclass(order=True)
class _Role:
    position: int
    id: int = field(compare=False)
    name: str = field(default="Role", compare=False)
    managed: bool = field(default=False, compare=False)
    admin: bool = field(default=False, compare=False)

    def __post_init__(self):
        self.permissions = SimpleNamespace(administrator=self.admin)
        self.color = SimpleNamespace(value=0)
        self.members = []

    def is_default(self):
        return False


class _Guild:
    """Guild stub that can answer both role lookups and member lookups."""

    def __init__(self, guild_id, roles, member_bits):
        self.id = guild_id
        self.name = "Home"
        self.roles = roles
        self._by_id = {r.id: r for r in roles}
        self.me = SimpleNamespace(
            top_role=_Role(position=100, id=1, name="DK"),
            guild_permissions=SimpleNamespace(manage_roles=True),
        )
        self._member = SimpleNamespace(
            id=1,
            display_name="tester",
            status="online",
            guild_permissions=SimpleNamespace(value=member_bits),
            roles=[],
        )

    def get_role(self, rid):
        return self._by_id.get(rid)

    def get_member(self, _uid):
        return self._member

    def get_channel_or_thread(self, _cid):
        return None


def _menu_client(fake_ctx, bits: int):
    """Client authenticated at ``bits``, with a guild holding one admin role."""
    roles = [
        _Role(position=50, id=500, name="Colors"),
        _Role(position=60, id=600, name="Staff", admin=True),
    ]
    guild = _Guild(fake_ctx.guild_id, roles, bits)
    fake_ctx.bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == fake_ctx.guild_id else None
    )
    _auth, client = _auth_client(fake_ctx, bits=bits)
    return client


def _menu_body(role_id: int, elevated: bool) -> dict:
    return {
        "title": "Pick a role",
        "description": "",
        "accent": "",
        "thumbnail_url": "",
        "style": "buttons",
        "mode": "toggle",
        "max_roles": 0,
        "required_role_id": "0",
        "cooldown_seconds": 0,
        "placeholder": "",
        "options": [
            {
                "role_id": str(role_id),
                "label": "Take it",
                "emoji": "",
                "description": "",
                "button_color": "secondary",
                "elevated": elevated,
            }
        ],
    }


def test_moderator_cannot_self_serve_an_admin_role(fake_ctx):
    """The ``elevated`` flag is supplied by the same moderator — it can't be the gate."""
    client = _menu_client(fake_ctx, _MOD_BITS)
    menu = client.post("/api/role-menus", json={"title": "Escalate"}).json()

    resp = client.put(f"/api/role-menus/{menu['id']}", json=_menu_body(600, True))
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"]

    # And nothing was written — the menu still has no options.
    assert client.get(f"/api/role-menus/{menu['id']}").json()["options"] == []
    client.close()


def test_moderator_may_still_edit_a_plain_role_menu(fake_ctx):
    client = _menu_client(fake_ctx, _MOD_BITS)
    menu = client.post("/api/role-menus", json={"title": "Colors"}).json()
    resp = client.put(f"/api/role-menus/{menu['id']}", json=_menu_body(500, False))
    assert resp.status_code == 200, resp.text
    assert resp.json()["menu"]["options"][0]["role_id"] == "500"
    client.close()


def test_admin_may_use_an_elevated_role(fake_ctx):
    client = _menu_client(fake_ctx, _ADMIN_BITS)
    menu = client.post("/api/role-menus", json={"title": "Staff"}).json()
    resp = client.put(f"/api/role-menus/{menu['id']}", json=_menu_body(600, True))
    assert resp.status_code == 200, resp.text
    assert resp.json()["menu"]["options"][0]["elevated"] is True
    client.close()


def test_elevated_override_is_still_audited_for_admins(fake_ctx):
    client = _menu_client(fake_ctx, _ADMIN_BITS)
    menu = client.post("/api/role-menus", json={"title": "Staff"}).json()
    client.put(f"/api/role-menus/{menu['id']}", json=_menu_body(600, True))
    with open_db(fake_ctx.db_path) as conn:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_log WHERE guild_id = ?", (fake_ctx.guild_id,)
            ).fetchall()
        ]
    assert "role_menu.elevated_override" in actions
    client.close()


def test_moderator_cannot_publish_an_admin_role_menu(fake_ctx):
    """A menu authored while the hole was open must not be publishable by a mod."""
    admin_client = _menu_client(fake_ctx, _ADMIN_BITS)
    menu = admin_client.post("/api/role-menus", json={"title": "Staff"}).json()
    admin_client.put(f"/api/role-menus/{menu['id']}", json=_menu_body(600, True))
    admin_client.close()

    mod_client = _menu_client(fake_ctx, _MOD_BITS)
    resp = mod_client.post(
        f"/api/role-menus/{menu['id']}/publish", json={"channel_id": "77"}
    )
    assert resp.status_code == 403
    mod_client.close()


def test_moderator_cannot_re_enable_an_admin_role_menu_but_can_disable_it(fake_ctx):
    admin_client = _menu_client(fake_ctx, _ADMIN_BITS)
    menu = admin_client.post("/api/role-menus", json={"title": "Staff"}).json()
    admin_client.put(f"/api/role-menus/{menu['id']}", json=_menu_body(600, True))
    admin_client.close()

    mod_client = _menu_client(fake_ctx, _MOD_BITS)
    off = mod_client.put(
        f"/api/role-menus/{menu['id']}/enabled", json={"enabled": False}
    )
    assert off.status_code == 200, off.text
    on = mod_client.put(f"/api/role-menus/{menu['id']}/enabled", json={"enabled": True})
    assert on.status_code == 403
    mod_client.close()


def test_menu_list_returns_options_for_every_menu(fake_ctx):
    """Guards the bulk-options query that replaced the per-menu N+1 (B-PERF7)."""
    client = _menu_client(fake_ctx, _ADMIN_BITS)
    first = client.post("/api/role-menus", json={"title": "One"}).json()
    second = client.post("/api/role-menus", json={"title": "Two"}).json()
    client.put(f"/api/role-menus/{first['id']}", json=_menu_body(500, False))
    client.put(f"/api/role-menus/{second['id']}", json=_menu_body(600, True))

    listed = {m["id"]: m for m in client.get("/api/role-menus").json()["menus"]}
    assert [o["role_id"] for o in listed[first["id"]]["options"]] == ["500"]
    assert [o["role_id"] for o in listed[second["id"]]["options"]] == ["600"]
    assert listed[second["id"]]["options"][0]["elevated"] is True
    client.close()


# ── B-SEC2 / B-PERF2 — regex search rails ────────────────────────────


def _seed_messages(db_path, guild_id, rows):
    with open_db(db_path) as conn:
        for i, content in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO messages (message_id, guild_id, channel_id, author_id,"
                " content, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (i, guild_id, 10, 100, content, 1000 + i),
            )


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param("(a+)+$", id="nested-quantifier"),
        pytest.param("([a-zA-Z]+)*!", id="nested-quantifier-class"),
        pytest.param("(x|xy)*z", id="ambiguous-alternation"),
        pytest.param("a{5000}", id="huge-repeat"),
        pytest.param("a" * 400, id="over-long-pattern"),
    ],
)
def test_catastrophic_regex_is_refused_not_run(authed_client, fake_ctx, pattern):
    """The classic ReDoS shapes come back as 400s instead of pinning the loop.

    This has to be refused at compile time: the dashboard shares the bot's
    process and CPython's ``re`` engine holds the GIL for the whole of one
    match, so "let it run and time out" is not available to us. The seeded
    row is the adversarial one — ``"aaa…a!"`` makes ``(a+)+$`` fail, and a
    failing match is what backtracks forever (before this fix that single row
    ran for >10s and counting, on the bot's own thread).
    """
    _seed_messages(fake_ctx.db_path, fake_ctx.guild_id, ["a" * 60 + "!"])
    resp = authed_client.get("/api/messages/search", params={"regex": pattern})
    assert resp.status_code == 400, resp.text


def test_ordinary_regexes_still_work(authed_client, fake_ctx):
    _seed_messages(
        fake_ctx.db_path, fake_ctx.guild_id, ["hello world", "goodbye moon", "hell no"]
    )
    body = authed_client.get(
        "/api/messages/search", params={"regex": r"hell(o|_)? w\w+"}
    ).json()
    assert {m["content"] for m in body["messages"]} == {"hello world"}
    assert "truncated" not in body  # only set when a rail stopped the scan


def test_regex_scan_abandons_when_it_blows_the_time_budget(
    authed_client, fake_ctx, monkeypatch
):
    """A scan that can't finish says so rather than holding the event loop."""
    from web_server.routes import messages as messages_routes

    _seed_messages(fake_ctx.db_path, fake_ctx.guild_id, ["needle", "haystack"])
    monkeypatch.setattr(messages_routes, "REGEX_TIME_BUDGET", -1.0)

    resp = authed_client.get("/api/messages/search", params={"regex": "needle"})
    assert resp.status_code == 400
    assert "narrow your filters" in resp.json()["detail"]


def test_regex_scan_is_bounded_and_reports_truncation(
    authed_client, fake_ctx, monkeypatch
):
    from web_server.routes import messages as messages_routes

    _seed_messages(fake_ctx.db_path, fake_ctx.guild_id, ["match me"] * 10)
    monkeypatch.setattr(messages_routes, "REGEX_MAX_MATCHES", 3)
    monkeypatch.setattr(messages_routes, "REGEX_CHUNK", 2)

    body = authed_client.get(
        "/api/messages/search", params={"regex": "match"}
    ).json()
    assert body["total"] == 3
    assert body["truncated"] is True


class _RecordingCursor:
    """Cursor stub that refuses ``fetchall`` — the thing B-PERF2 was about."""

    def __init__(self, rows, chunk_sizes):
        self._rows = list(rows)
        self._pos = 0
        self.chunk_sizes = chunk_sizes

    def fetchall(self):  # pragma: no cover - must never be reached
        raise AssertionError("regex scan must stream, not materialize everything")

    def fetchmany(self, size):
        self.chunk_sizes.append(size)
        window = self._rows[self._pos : self._pos + size]
        self._pos += len(window)
        return window


def test_regex_scan_streams_and_keeps_only_matches():
    """The old code ``fetchall()``'d a no-LIMIT result set with content attached."""
    from web_server.routes import messages as messages_routes

    rows = [(i, 10, 100, "keep" if i % 2 else "drop", None, 0, 0, None) for i in range(9)]
    sizes: list[int] = []
    cursor = _RecordingCursor(rows, sizes)

    matched, scanned, capped = messages_routes.scan_regex_rows(
        cursor,
        messages_routes.compile_search_regex("keep"),
        messages_routes._regex_deadline(),
        messages_routes.REGEX_MAX_MATCHES,
    )
    assert scanned == 9
    assert capped is False
    assert [r[0] for r in matched] == [1, 3, 5, 7]
    assert sizes and all(s == messages_routes.REGEX_CHUNK for s in sizes)


def test_regex_search_returns_only_matching_rows(authed_client, fake_ctx):
    _seed_messages(fake_ctx.db_path, fake_ctx.guild_id, ["keep", "drop", "keep me"])
    body = authed_client.get("/api/messages/search", params={"regex": "keep"}).json()
    assert {m["content"] for m in body["messages"]} == {"keep", "keep me"}


def test_export_refuses_a_catastrophic_regex(authed_client, fake_ctx):
    _seed_messages(fake_ctx.db_path, fake_ctx.guild_id, ["a" * 60 + "!"])
    resp = authed_client.get("/api/messages/search/export", params={"regex": "(a+)+$"})
    assert resp.status_code == 400


# ── B-SEC7 — top channels respect what the viewer can see ────────────


class _HomeGuild:
    def __init__(self, guild_id, visible_ids, member_bits=0):
        self.id = guild_id
        self.name = "Home"
        self.member_count = 1
        self.icon = None
        self.members = []
        self.voice_channels = []
        self.channels = []
        self._visible = visible_ids
        self.member_bits = member_bits

    def get_member(self, _uid):
        return SimpleNamespace(
            id=1,
            display_name="viewer",
            status="online",
            bot=False,
            guild_permissions=SimpleNamespace(value=self.member_bits),
            roles=[],
        )

    def _channel(self, cid):
        return SimpleNamespace(
            id=cid,
            name=f"channel-{cid}",
            permissions_for=lambda _m, _cid=cid: SimpleNamespace(
                view_channel=_cid in self._visible
            ),
        )

    def get_channel_or_thread(self, cid):
        return self._channel(cid)

    def get_channel(self, cid):
        return self._channel(cid)


def test_non_mod_home_hides_channels_they_cannot_see(fake_ctx):
    """A member shouldn't learn #staff-only exists from the busiest-rooms board."""
    _seed_messages(fake_ctx.db_path, fake_ctx.guild_id, [])
    with open_db(fake_ctx.db_path) as conn:
        import time as _time

        now = int(_time.time())
        for i, cid in enumerate([11, 11, 22], start=1):
            conn.execute(
                "INSERT INTO messages (message_id, guild_id, channel_id, author_id,"
                " content, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (i, fake_ctx.guild_id, cid, 100, "hi", now),
            )

    def _wire(bits):
        guild = _HomeGuild(fake_ctx.guild_id, visible_ids={11}, member_bits=bits)
        fake_ctx.bot = SimpleNamespace(
            get_guild=lambda gid: guild if gid == fake_ctx.guild_id else None
        )

    _wire(0)
    _auth, member_client = _auth_client(fake_ctx, bits=0)
    body = member_client.get("/api/home?fields=top_channels").json()
    assert [c["channel_id"] for c in body["top_channels"]] == ["11"]
    member_client.close()

    _wire(_MOD_BITS)
    _auth, mod_client = _auth_client(fake_ctx, bits=_MOD_BITS)
    mod_body = mod_client.get("/api/home?fields=top_channels").json()
    assert {c["channel_id"] for c in mod_body["top_channels"]} == {"11", "22"}
    mod_client.close()


def test_non_mod_home_hides_all_channels_when_the_bot_is_offline(fake_ctx):
    """No member cache means no way to evaluate visibility — fail closed."""
    with open_db(fake_ctx.db_path) as conn:
        import time as _time

        conn.execute(
            "INSERT INTO messages (message_id, guild_id, channel_id, author_id,"
            " content, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (1, fake_ctx.guild_id, 11, 100, "hi", int(_time.time())),
        )
    _auth, client = _auth_client(fake_ctx, bits=0)
    assert client.get("/api/home?fields=top_channels").json()["top_channels"] == []
    client.close()


# ── B-SEC8 — wellness cap snowflakes survive JSON ────────────────────


def test_cap_scope_target_id_is_a_string(authed_client, fake_ctx):
    """Channel snowflakes exceed 2^53; as a bare number JS silently rounds them."""
    big = 1401234567890123456
    with open_db(fake_ctx.db_path) as conn:
        opt_in_user(conn, fake_ctx.guild_id, 1, timezone="UTC")
        add_cap(
            conn,
            fake_ctx.guild_id,
            user_id=1,
            label="Channel cap",
            scope="channel",
            scope_target_id=big,
            window="daily",
            cap_limit=10,
        )

    raw = authed_client.get("/api/wellness/caps").text
    assert f'"scope_target_id": "{big}"' in raw or f'"scope_target_id":"{big}"' in raw
    cap = authed_client.get("/api/wellness/caps").json()["caps"][0]
    assert cap["scope_target_id"] == str(big)


def test_cap_write_accepts_a_string_scope_target_id(authed_client, fake_ctx):
    """The read side stringifies, so the write side has to take strings back."""
    big = "1401234567890123456"
    with open_db(fake_ctx.db_path) as conn:
        opt_in_user(conn, fake_ctx.guild_id, 1, timezone="UTC")

    resp = authed_client.post(
        "/api/wellness/caps",
        json={
            "label": "Channel cap",
            "scope": "channel",
            "scope_target_id": big,
            "window": "daily",
            "limit": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    cap = authed_client.get("/api/wellness/caps").json()["caps"][0]
    assert cap["scope_target_id"] == big


# ── B-SEC6 — the Spotify error branch escapes its input ──────────────


def test_spotify_callback_escapes_the_error_param(open_client):
    open_client.cookies.set("dk_spotify_oauth_state", "s")
    resp = open_client.get(
        "/spotify/callback",
        params={"state": "s", "error": "<img src=x onerror=alert(1)>"},
    )
    assert resp.status_code == 400
    assert "<img" not in resp.text
    assert "&lt;img" in resp.text
