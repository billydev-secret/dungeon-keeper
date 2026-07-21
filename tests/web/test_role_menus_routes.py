"""Web route tests for Role Menus (/api/role-menus).

The heavier guild-dependent paths (publish, live sync) are exercised against a
fake guild wired onto the FakeCtx; pure-DB paths run without a bot, mirroring
how the dashboard behaves when Discord is down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace


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
    def __init__(self, guild_id, roles):
        self.id = guild_id
        self.roles = roles
        self._by_id = {r.id: r for r in roles}
        self.me = SimpleNamespace(
            top_role=_Role(position=100, id=1, name="DK"),
            guild_permissions=SimpleNamespace(manage_roles=True),
        )

    def get_role(self, rid):
        return self._by_id.get(rid)

    def get_channel_or_thread(self, _cid):
        return None


def _wire_guild(fake_ctx, roles):
    guild = _Guild(fake_ctx.guild_id, roles)
    fake_ctx.bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == fake_ctx.guild_id else None
    )
    return guild


def _create(client, title="Colors"):
    r = client.post("/api/role-menus", json={"title": title})
    assert r.status_code == 200, r.text
    return r.json()


def test_create_list_get_roundtrip(open_client):
    menu = _create(open_client)
    assert menu["title"] == "Colors" and menu["published"] is False

    listed = open_client.get("/api/role-menus").json()["menus"]
    assert [m["id"] for m in listed] == [menu["id"]]

    got = open_client.get(f"/api/role-menus/{menu['id']}").json()
    assert got["style"] == "buttons" and got["mode"] == "toggle"
    assert got["options"] == []


def test_get_missing_menu_404s(open_client):
    assert open_client.get("/api/role-menus/9999").status_code == 404


def test_update_validation_rejects_bad_payloads(open_client):
    menu = _create(open_client)
    base = {
        "title": "Colors", "description": "", "accent": "", "thumbnail_url": "",
        "style": "buttons", "mode": "toggle", "max_roles": 0,
        "required_role_id": "0", "cooldown_seconds": 0, "placeholder": "",
    }
    url = f"/api/role-menus/{menu['id']}"

    r = open_client.put(url, json={**base, "style": "reactions"})
    assert r.status_code == 400  # explicitly not a reaction system

    r = open_client.put(url, json={**base, "mode": "temporary"})
    assert r.status_code == 400

    dup = [{"role_id": "11", "label": "A"}, {"role_id": "11", "label": "B"}]
    r = open_client.put(url, json={**base, "options": dup})
    assert r.status_code == 400 and "twice" in r.json()["detail"]

    unlabeled = [{"role_id": "11", "label": "  "}]
    r = open_client.put(url, json={**base, "options": unlabeled})
    assert r.status_code == 400 and "label" in r.json()["detail"]

    too_many = [{"role_id": str(i), "label": f"r{i}"} for i in range(1, 27)]
    r = open_client.put(url, json={**base, "options": too_many})
    assert r.status_code == 400 and "25" in r.json()["detail"]


def test_update_without_bot_is_503(open_client):
    # Valid payloads still need the guild for role checks: bot down → 503.
    menu = _create(open_client)
    r = open_client.put(f"/api/role-menus/{menu['id']}", json={
        "title": "Colors", "description": "", "accent": "", "thumbnail_url": "",
        "style": "buttons", "mode": "toggle", "max_roles": 0,
        "required_role_id": "0", "cooldown_seconds": 0, "placeholder": "",
        "options": [{"role_id": "11", "label": "Red"}],
    })
    assert r.status_code == 503


def test_update_saves_options_and_audits(open_client, fake_ctx):
    _wire_guild(fake_ctx, [
        _Role(position=5, id=11, name="Red"),
        _Role(position=6, id=22, name="Blue"),
    ])
    menu = _create(open_client)
    r = open_client.put(f"/api/role-menus/{menu['id']}", json={
        "title": "Colors", "description": "Pick!", "accent": "#ff0000",
        "thumbnail_url": "", "style": "dropdown", "mode": "unique",
        "max_roles": 0, "required_role_id": "0", "cooldown_seconds": 5,
        "placeholder": "Choose…",
        "options": [
            {"role_id": "22", "label": "Blue", "emoji": "🔵"},
            {"role_id": "11", "label": "Red", "button_color": "danger"},
        ],
    })
    assert r.status_code == 200, r.text
    saved = r.json()["menu"]
    assert [o["role_id"] for o in saved["options"]] == ["22", "11"]  # order kept
    assert saved["mode"] == "unique" and r.json()["sync"] is None  # draft: no sync

    with fake_ctx.open_db() as conn:
        actions = [r2["action"] for r2 in conn.execute(
            "SELECT action FROM audit_log WHERE guild_id = ?", (fake_ctx.guild_id,)
        ).fetchall()]
    assert "role_menu.create" in actions and "role_menu.update" in actions


def test_dangerous_role_needs_elevated_override(open_client, fake_ctx):
    _wire_guild(fake_ctx, [_Role(position=5, id=66, name="Mods", admin=True)])
    menu = _create(open_client)
    base = {
        "title": "Colors", "description": "", "accent": "", "thumbnail_url": "",
        "style": "buttons", "mode": "toggle", "max_roles": 0,
        "required_role_id": "0", "cooldown_seconds": 0, "placeholder": "",
    }
    url = f"/api/role-menus/{menu['id']}"

    r = open_client.put(url, json={
        **base, "options": [{"role_id": "66", "label": "Mods"}],
    })
    assert r.status_code == 400 and "elevated" in r.json()["detail"]

    r = open_client.put(url, json={
        **base, "options": [{"role_id": "66", "label": "Mods", "elevated": True}],
    })
    assert r.status_code == 200, r.text

    with fake_ctx.open_db() as conn:
        actions = [row["action"] for row in conn.execute(
            "SELECT action FROM audit_log WHERE action = 'role_menu.elevated_override'"
        ).fetchall()]
    assert actions == ["role_menu.elevated_override"]  # logged loudly


def test_unmanageable_roles_are_refused(open_client, fake_ctx):
    _wire_guild(fake_ctx, [
        _Role(position=200, id=70, name="Above DK"),   # above bot top role
        _Role(position=5, id=71, name="Booster", managed=True),
    ])
    menu = _create(open_client)
    base = {
        "title": "x", "description": "", "accent": "", "thumbnail_url": "",
        "style": "buttons", "mode": "toggle", "max_roles": 0,
        "required_role_id": "0", "cooldown_seconds": 0, "placeholder": "",
    }
    url = f"/api/role-menus/{menu['id']}"

    r = open_client.put(url, json={**base, "options": [{"role_id": "70", "label": "A"}]})
    assert r.status_code == 400 and "highest role" in r.json()["detail"]

    r = open_client.put(url, json={**base, "options": [{"role_id": "71", "label": "B"}]})
    assert r.status_code == 400 and "integration" in r.json()["detail"]

    r = open_client.put(url, json={**base, "options": [{"role_id": "72", "label": "C"}]})
    assert r.status_code == 400  # unknown role id


def test_roles_endpoint_flags_dangerous_and_assignable(open_client, fake_ctx):
    _wire_guild(fake_ctx, [
        _Role(position=5, id=11, name="Red"),
        _Role(position=200, id=70, name="Above DK"),
        _Role(position=6, id=66, name="Mods", admin=True),
        _Role(position=7, id=71, name="Booster", managed=True),
    ])
    r = open_client.get("/api/role-menus/roles")
    assert r.status_code == 200
    roles = {x["name"]: x for x in r.json()["roles"]}
    assert "Booster" not in roles  # managed roles never appear
    assert roles["Red"]["assignable"] and not roles["Red"]["dangerous"]
    assert not roles["Above DK"]["assignable"]
    assert roles["Mods"]["dangerous"]


def test_delete_removes_menu_but_keeps_grants(open_client, fake_ctx):
    from bot_modules.role_menus import db as menus_db

    menu = _create(open_client)
    with fake_ctx.open_db() as conn:
        menus_db.record_grants(conn, menu["id"], fake_ctx.guild_id, 555,
                               [(11, "grant")], 1.0)

    r = open_client.delete(f"/api/role-menus/{menu['id']}")
    assert r.status_code == 200
    assert open_client.get("/api/role-menus").json()["menus"] == []

    with fake_ctx.open_db() as conn:
        kept = conn.execute(
            "SELECT COUNT(*) AS n FROM role_menu_grants WHERE menu_id = ?",
            (menu["id"],),
        ).fetchone()["n"]
    assert kept == 1
