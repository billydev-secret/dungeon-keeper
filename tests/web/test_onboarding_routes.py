"""Tests for /api/onboarding — reading Discord's onboarding and adding roles.

Route-layer behaviour the planning suite can't see: the four role states the
panel renders from, snowflakes leaving as strings, the degraded paths when the
bot is offline or lacks Manage Server, and — the one that matters most — that
the write is planned against a **freshly read** config rather than whatever the
panel was showing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

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


def _option(title, *role_ids, description="", emoji=None):
    o = MagicMock()
    o.title = title
    o.description = description
    o.emoji = emoji
    o.role_ids = list(role_ids)
    o.channel_ids = []
    return o


def _prompt(pid, title, options=()):
    p = MagicMock()
    p.id = pid
    p.title = title
    p.type = MagicMock(name="multiple_choice")
    p.type.name = "multiple_choice"
    p.single_select = False
    p.required = False
    p.in_onboarding = True
    p.options = list(options)
    return p


def _attach(fake_ctx, *, prompts=(), roles=(), can_edit=True, edit=None):
    member = _auth_member()
    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.roles = list(roles)
    role_by_id = {r.id: r for r in roles}
    guild.get_role = MagicMock(side_effect=role_by_id.get)
    guild.get_member = MagicMock(
        side_effect=lambda uid: member if int(uid) == 1 else None
    )
    guild.me = MagicMock()
    guild.me.id = 42
    guild.me.guild_permissions = MagicMock(
        manage_guild=can_edit, manage_roles=can_edit
    )
    onboarding = MagicMock()
    onboarding.prompts = list(prompts)
    guild.onboarding = AsyncMock(return_value=onboarding)
    guild.edit_onboarding = edit or AsyncMock()
    guild.create_role = AsyncMock(
        side_effect=lambda name, **kw: MagicMock(id=BIG_ROLE_ID, name=name)
    )
    bot = MagicMock()
    bot.get_guild = MagicMock(
        side_effect=lambda gid: guild if gid == fake_ctx.guild_id else None
    )
    fake_ctx.bot = bot
    return guild


def _role(rid, name):
    r = MagicMock()
    r.id = rid
    r.name = name
    return r


# ── reading ──────────────────────────────────────────────────────────


def test_get_reports_the_live_prompts(authed_client, fake_ctx):
    _attach(fake_ctx, prompts=[_prompt(111, "Pick pings", [_option("A", 222)])])
    body = authed_client.get("/api/onboarding").json()

    assert [p["title"] for p in body["prompts"]] == ["Pick pings"]
    assert body["prompts"][0]["options"][0]["title"] == "A"


def test_ids_leave_as_strings(authed_client, fake_ctx):
    _attach(fake_ctx, prompts=[_prompt(BIG_ROLE_ID, "P", [_option("A", BIG_ROLE_ID)])])
    body = authed_client.get("/api/onboarding").json()

    assert body["prompts"][0]["id"] == str(BIG_ROLE_ID)
    assert body["prompts"][0]["options"][0]["role_ids"] == [str(BIG_ROLE_ID)]


def test_role_states_tell_the_four_cases_apart(authed_client, fake_ctx):
    """What the panel's checkboxes and badges are built from."""
    ready = _role(555, "Risky Rolls")
    offered = _role(666, "QOTD")
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "risky_ping_role_id", "555", fake_ctx.guild_id)
        set_config_value(conn, "econ_qotd_ping_role_id", "666", fake_ctx.guild_id)
        # An admin who chose "(none)" — never overridden.
        set_config_value(conn, "welcome_ping_role_id", "0", fake_ctx.guild_id)
        conn.commit()
    _attach(
        fake_ctx,
        roles=[ready, offered],
        prompts=[_prompt(1, "P", [_option("QOTD", 666)])],
    )

    states = {r["key"]: r["state"] for r in authed_client.get("/api/onboarding").json()["roles"]}
    assert states["risky_ping_role_id"] == "ready"
    assert states["econ_qotd_ping_role_id"] == "offered"
    assert states["welcome_ping_role_id"] == "off"
    # Never configured and no role yet — we'd make it on add.
    assert states["promotion_review_ping_role_id"] == "uncreated"


def test_can_edit_is_false_without_manage_server(authed_client, fake_ctx):
    _attach(fake_ctx, can_edit=False)
    assert authed_client.get("/api/onboarding").json()["can_edit"] is False


def test_bot_offline_is_a_503_not_a_crash(authed_client, fake_ctx):
    fake_ctx.bot = None
    assert authed_client.get("/api/onboarding").status_code == 503


def test_missing_manage_server_on_read_is_a_403(authed_client, fake_ctx):
    guild = _attach(fake_ctx)
    guild.onboarding = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "nope")
    )
    assert authed_client.get("/api/onboarding").status_code == 403


# ── writing ──────────────────────────────────────────────────────────


def test_adding_a_role_provisions_it_and_writes_onboarding(authed_client, fake_ctx):
    guild = _attach(fake_ctx, prompts=[_prompt(111, "Pick pings")])

    resp = authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["risky_ping_role_id"], "prompt_id": "111"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"] is True
    assert resp.json()["added"] == ["Risky Rolls"]

    guild.create_role.assert_awaited()          # it didn't exist yet
    guild.edit_onboarding.assert_awaited_once()
    written = guild.edit_onboarding.await_args.kwargs["prompts"]
    assert [o.title for o in written[0].options] == ["Risky Rolls"]


def test_the_provisioned_id_is_persisted(authed_client, fake_ctx):
    _attach(fake_ctx, prompts=[_prompt(111, "P")])
    authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["risky_ping_role_id"], "prompt_id": "111"},
    )
    from bot_modules.core.db_utils import get_config_value

    with open_db(fake_ctx.db_path) as conn:
        assert get_config_value(
            conn, "risky_ping_role_id", "0", fake_ctx.guild_id
        ) == str(BIG_ROLE_ID)


def test_untargeted_prompts_survive_the_write(authed_client, fake_ctx):
    """edit_onboarding replaces everything, so an unrelated question has to be
    written back exactly as it was."""
    guild = _attach(fake_ctx, prompts=[
        _prompt(111, "Pick pings"),
        _prompt(222, "Pick colours", [_option("Red", 999, description="warm", emoji="🔴")]),
    ])
    authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["risky_ping_role_id"], "prompt_id": "111"},
    )

    written = guild.edit_onboarding.await_args.kwargs["prompts"]
    assert len(written) == 2
    colours = written[1]
    assert colours.title == "Pick colours"
    assert [o.title for o in colours.options] == ["Red"]
    # discord.py stores role ids as a set on the option object.
    assert set(colours.options[0].role_ids) == {999}
    assert colours.options[0].description == "warm"


def test_a_role_switched_off_is_never_provisioned(authed_client, fake_ctx):
    """An admin's "(none)" beats a request to add it.

    Risky Rolls, not the economy dial: that one treats a stored 0 as a save
    artifact and provisions anyway — see the next test.
    """
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "risky_ping_role_id", "0", fake_ctx.guild_id)
        conn.commit()
    guild = _attach(fake_ctx, prompts=[_prompt(111, "P")])

    resp = authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["risky_ping_role_id"], "prompt_id": "111"},
    )
    assert resp.status_code == 422
    guild.create_role.assert_not_awaited()
    guild.edit_onboarding.assert_not_awaited()


def test_an_unknown_key_is_refused(authed_client, fake_ctx):
    guild = _attach(fake_ctx, prompts=[_prompt(111, "P")])
    resp = authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["admin_role_ids"], "prompt_id": "111"},
    )
    assert resp.status_code == 400
    guild.edit_onboarding.assert_not_awaited()


def test_a_stale_prompt_id_is_refused_without_writing(authed_client, fake_ctx):
    """The panel loaded, someone edited onboarding in Server Settings, then the
    admin saved. Writing would put the roles in the wrong question."""
    guild = _attach(fake_ctx, prompts=[_prompt(111, "P")])
    resp = authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["risky_ping_role_id"], "prompt_id": "999"},
    )
    assert resp.status_code == 422
    guild.edit_onboarding.assert_not_awaited()
    # ...and it must not have left a freshly-made role behind for a request it
    # was never going to honour.
    guild.create_role.assert_not_awaited()


def test_an_already_offered_role_writes_nothing(authed_client, fake_ctx):
    existing = _role(555, "Risky Rolls")
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "risky_ping_role_id", "555", fake_ctx.guild_id)
        conn.commit()
    guild = _attach(
        fake_ctx, roles=[existing],
        prompts=[_prompt(111, "P", [_option("Risky Rolls", 555)])],
    )
    resp = authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["risky_ping_role_id"], "prompt_id": "111"},
    )
    assert resp.status_code == 200
    assert resp.json()["written"] is False
    guild.edit_onboarding.assert_not_awaited()


def test_forbidden_on_write_is_reported_as_403(authed_client, fake_ctx):
    _attach(
        fake_ctx, prompts=[_prompt(111, "P")],
        edit=AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no")),
    )
    resp = authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["risky_ping_role_id"], "prompt_id": "111"},
    )
    assert resp.status_code == 403


def test_a_successful_add_writes_an_audit_row(authed_client, fake_ctx):
    _attach(fake_ctx, prompts=[_prompt(111, "P")])
    authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["risky_ping_role_id"], "prompt_id": "111"},
    )
    with open_db(fake_ctx.db_path) as conn:
        rows = conn.execute(
            "SELECT action FROM audit_log WHERE guild_id = ?", (fake_ctx.guild_id,)
        ).fetchall()
    assert any(r["action"] == "onboarding_roles_added" for r in rows)


def test_the_economy_dial_is_addable_despite_a_stored_zero(authed_client, fake_ctx):
    """Economy Settings writes "0" for an untouched picker on every save, so a 0
    there records a save rather than a decision — and an opt-in role with no
    role is just a broken button. It stays addable."""
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "econ_game_role_id", "0", fake_ctx.guild_id)
        set_config_value(conn, "risky_ping_role_id", "0", fake_ctx.guild_id)
        conn.commit()
    guild = _attach(fake_ctx, prompts=[_prompt(111, "P")])

    states = {
        r["key"]: r["state"]
        for r in authed_client.get("/api/onboarding").json()["roles"]
    }
    assert states["econ_game_role_id"] == "uncreated"
    assert states["risky_ping_role_id"] == "off", "the exception is scoped to one dial"

    resp = authed_client.post(
        "/api/onboarding/add-roles",
        json={"keys": ["econ_game_role_id"], "prompt_id": "111"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] == ["Economy Notifications"]
    guild.edit_onboarding.assert_awaited_once()
