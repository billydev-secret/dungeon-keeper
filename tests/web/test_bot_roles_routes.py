"""Tests for /api/bot-roles — the roster, and its three narrow writes.

The state machine itself is covered pure in
``tests/test_role_roster_service.py``; what only the route can prove is that it
reads each family of dial the way the feature that owns it reads it, that
snowflakes leave as strings, and that the three writes refuse everything they
are supposed to refuse — which is most of the safety in this feature.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bot_modules.core.db_utils import open_db, set_config_value

# Above 2^53: JS Number would mangle it.
BIG_ROLE_ID = 987654321098765432


def _auth_member():
    m = MagicMock()
    m.id = 1
    m.bot = False
    m.guild_permissions = MagicMock(value=0x8, administrator=True)
    m.display_name = "tester"
    default_role = MagicMock(id=0)
    default_role.is_default = MagicMock(return_value=True)
    m.roles = [default_role]
    return m


def _role(rid, name, *, position=5, managed=False, members=0):
    r = MagicMock()
    r.id = rid
    r.name = name
    r.position = position
    r.managed = managed
    r.members = [MagicMock() for _ in range(members)]
    return r


def _attach(fake_ctx, *, roles=(), bot_top=50, manage_roles=True, features=()):
    member = _auth_member()
    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.roles = list(roles)
    by_id = {r.id: r for r in roles}
    guild.get_role = MagicMock(side_effect=by_id.get)
    guild.get_member = MagicMock(
        side_effect=lambda uid: member if int(uid) == 1 else None
    )
    guild.features = list(features)
    guild.me = MagicMock()
    guild.me.id = 42
    guild.me.top_role = MagicMock(position=bot_top)
    guild.me.guild_permissions = MagicMock(
        manage_roles=manage_roles, manage_guild=True
    )
    guild.create_role = AsyncMock(
        side_effect=lambda name, **kw: _role(BIG_ROLE_ID, name, position=1)
    )
    bot = MagicMock()
    bot.get_guild = MagicMock(
        side_effect=lambda gid: guild if gid == fake_ctx.guild_id else None
    )
    fake_ctx.bot = bot
    return guild


def _by_key(body):
    return {r["key"]: r for r in body["roles"]}


# ── reading ──────────────────────────────────────────────────────────


def test_the_roster_lists_every_managed_role(authed_client, fake_ctx):
    """The whole point of the page: the nine roles features make on their own
    were unlistable before this, so a missing one was invisible until it
    failed."""
    from bot_modules.services import feature_roles as fr

    _attach(fake_ctx)
    body = authed_client.get("/api/bot-roles").json()

    assert set(_by_key(body)) == {e.key for e in fr.MANAGED_ROLES}


def test_ids_leave_as_strings(authed_client, fake_ctx):
    _attach(fake_ctx, roles=[_role(BIG_ROLE_ID, "Welcome Ping")])
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "welcome_ping_role_id", str(BIG_ROLE_ID),
                         fake_ctx.guild_id)
        conn.commit()

    card = _by_key(authed_client.get("/api/bot-roles").json())["welcome_ping_role_id"]
    assert card["role_id"] == str(BIG_ROLE_ID)


def test_an_inherited_dial_is_not_reported_as_a_deletion(authed_client, fake_ctx):
    """The live prod shape: `jailed_role_id` set at guild_id = 0 with guilds
    holding no row of their own."""
    _attach(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "jailed_role_id", "5000", 0)
        conn.commit()

    card = _by_key(authed_client.get("/api/bot-roles").json())["jailed_role_id"]
    assert card["state"] == "inherited"


def test_a_role_above_the_bot_is_out_of_reach_only_when_handed_out(
    authed_client, fake_ctx
):
    high_jail = _role(700, "Jailed", position=90)
    high_ping = _role(701, "Welcome Ping", position=91)
    _attach(fake_ctx, roles=[high_jail, high_ping], bot_top=10)
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "jailed_role_id", "700", fake_ctx.guild_id)
        set_config_value(conn, "welcome_ping_role_id", "701", fake_ctx.guild_id)
        conn.commit()

    cards = _by_key(authed_client.get("/api/bot-roles").json())
    assert cards["jailed_role_id"]["state"] == "out_of_reach"
    # Mentioning needs no hierarchy — a warning here would be crying wolf.
    assert cards["welcome_ping_role_id"]["state"] == "in_use"


def test_the_state_endpoint_answers_only_the_keys_a_panel_owns(
    authed_client, fake_ctx
):
    _attach(fake_ctx)
    body = authed_client.get(
        "/api/bot-roles/state?keys=welcome_ping_role_id"
    ).json()
    assert [r["key"] for r in body["roles"]] == ["welcome_ping_role_id"]


# ── create ───────────────────────────────────────────────────────────


def test_create_makes_the_role_and_records_that_it_did(authed_client, fake_ctx):
    from bot_modules.services.role_provenance import read_role_provenance

    _attach(fake_ctx)
    res = authed_client.post("/api/bot-roles/create",
                             json={"key": "welcome_ping_role_id"})
    assert res.status_code == 200
    assert res.json()["role_id"] == str(BIG_ROLE_ID)
    with open_db(fake_ctx.db_path) as conn:
        prov = read_role_provenance(conn, fake_ctx.guild_id)
    assert prov["welcome_ping_role_id"].origin == "created"


def test_create_refuses_a_create_on_offer_dial(authed_client, fake_ctx):
    """Billy's condition for reopening these two dials, enforced.

    Making @Guess Who from here without offering it to anybody is exactly the
    empty-role failure that kept the dial out of the registry: the game would
    read as configured and refuse every member.
    """
    guild = _attach(fake_ctx)
    res = authed_client.post("/api/bot-roles/create", json={"key": "guess_role_id"})

    assert res.status_code == 422
    assert "offer" in res.json()["detail"].lower()
    guild.create_role.assert_not_awaited()


def test_create_refuses_a_role_another_feature_owns(authed_client, fake_ctx):
    _attach(fake_ctx)
    res = authed_client.post("/api/bot-roles/create",
                             json={"key": "wellness_role_id"})
    assert res.status_code == 422


def test_create_refuses_an_unknown_key(authed_client, fake_ctx):
    _attach(fake_ctx)
    assert authed_client.post(
        "/api/bot-roles/create", json={"key": "mod_role_ids"}
    ).status_code == 404


# ── adopt ────────────────────────────────────────────────────────────


def test_adopt_points_the_dial_and_records_the_adoption(authed_client, fake_ctx):
    from bot_modules.core.db_utils import get_config_value
    from bot_modules.services.role_provenance import read_role_provenance

    _attach(fake_ctx, roles=[_role(600, "Announcements", position=3)])
    res = authed_client.post(
        "/api/bot-roles/adopt",
        json={"key": "welcome_ping_role_id", "role_id": "600"},
    )

    assert res.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        assert get_config_value(
            conn, "welcome_ping_role_id", "0", fake_ctx.guild_id
        ) == "600"
        assert read_role_provenance(
            conn, fake_ctx.guild_id
        )["welcome_ping_role_id"].origin == "adopted"


def test_adopt_refuses_an_integration_managed_role(authed_client, fake_ctx):
    _attach(fake_ctx, roles=[_role(600, "Server Booster", managed=True)])
    res = authed_client.post(
        "/api/bot-roles/adopt",
        json={"key": "welcome_ping_role_id", "role_id": "600"},
    )
    assert res.status_code == 422


def test_adopt_refuses_a_role_the_bot_could_never_grant(authed_client, fake_ctx):
    """The hierarchy check the provisioner never had. Storing this id would
    make every jail fail at add_roles with an error about a role the admin
    never chose."""
    _attach(fake_ctx, roles=[_role(600, "Warden", position=90)], bot_top=10)
    res = authed_client.post(
        "/api/bot-roles/adopt", json={"key": "jailed_role_id", "role_id": "600"},
    )
    assert res.status_code == 422
    assert "above my own role" in res.json()["detail"]


def test_adopt_allows_a_high_role_for_a_dial_the_bot_only_mentions(
    authed_client, fake_ctx
):
    _attach(fake_ctx, roles=[_role(600, "Announcements", position=90)], bot_top=10)
    res = authed_client.post(
        "/api/bot-roles/adopt",
        json={"key": "welcome_ping_role_id", "role_id": "600"},
    )
    assert res.status_code == 200


# ── stop ─────────────────────────────────────────────────────────────


def test_stop_unpoints_the_dial_and_never_deletes_the_role(
    authed_client, fake_ctx
):
    """"Stop managing" means stop pointing at it (Billy, 2026-09-03).

    Provenance would make a delete button safe to offer, which is not the same
    as wanting one — so the role stays in the server and everybody holding it
    keeps it.
    """
    from bot_modules.core.db_utils import get_config_value
    from bot_modules.services.role_provenance import read_role_provenance

    role = _role(600, "Welcome Ping", position=3)
    role.delete = AsyncMock()
    _attach(fake_ctx, roles=[role])
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "welcome_ping_role_id", "600", fake_ctx.guild_id)
        conn.commit()

    assert authed_client.post(
        "/api/bot-roles/stop", json={"key": "welcome_ping_role_id"}
    ).status_code == 200

    role.delete.assert_not_awaited()
    with open_db(fake_ctx.db_path) as conn:
        assert get_config_value(
            conn, "welcome_ping_role_id", "0", fake_ctx.guild_id
        ) == "0"
        assert "welcome_ping_role_id" not in read_role_provenance(
            conn, fake_ctx.guild_id
        )


def test_a_stopped_dial_then_reads_as_deliberately_off(authed_client, fake_ctx):
    """The stored "0" has to be the same "0" the provisioner honours, or
    "stop managing" would be undone by the feature's next run."""
    _attach(fake_ctx, roles=[_role(600, "Welcome Ping", position=3)])
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "welcome_ping_role_id", "600", fake_ctx.guild_id)
        conn.commit()
    authed_client.post("/api/bot-roles/stop", json={"key": "welcome_ping_role_id"})

    card = _by_key(authed_client.get("/api/bot-roles").json())["welcome_ping_role_id"]
    assert card["state"] == "turned_off"


def test_stop_refuses_a_dial_with_no_coherent_off(authed_client, fake_ctx):
    """A jail with no role is not a jail; switching it off would break the
    feature with nothing to show that had happened."""
    _attach(fake_ctx)
    res = authed_client.post("/api/bot-roles/stop", json={"key": "jailed_role_id"})
    assert res.status_code == 422
