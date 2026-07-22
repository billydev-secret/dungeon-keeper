"""Projector + color-guard tests for economy/perk_actions (Stage 3, Agent C).

Covers the ΔE staff-collision maths, hex parsing, feature gating, and the
personal-role projection matrix (solid / gradient-supersedes / name-default /
downgrade-clears / idempotent no-op / delete-when-empty).
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.economy.perk_actions import (
    apply_role_perks,
    delta_e_cie76,
    feature_gate_ok,
    find_color_clash,
    parse_hex_color,
    revoke_role_perks,
    should_revert_nick,
)
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_rentals_service import (
    get_personal_role,
    upsert_personal_role,
)
from migrations import apply_migrations_sync

GUILD_ID = 4242
USER_ID = 700


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "test.db"
    apply_migrations_sync(p)
    return p


def _add_rental(db, perk, *, user_id=USER_ID, beneficiary_id=None, state="active"):
    beneficiary_id = user_id if beneficiary_id is None else beneficiary_id
    now = time.time()
    with open_db(db) as conn:
        conn.execute(
            """
            INSERT INTO econ_rentals
                (guild_id, user_id, perk, state, price, started_at, next_bill_at,
                 cancel_at_period_end, suspended, beneficiary_id, created_at)
            VALUES (?, ?, ?, ?, 50, ?, ?, 0, 0, ?, ?)
            """,
            (GUILD_ID, user_id, perk, state, now, now + 604800, beneficiary_id, now),
        )


def _set_desired(db, **values):
    with open_db(db) as conn:
        upsert_personal_role(conn, GUILD_ID, USER_ID, values)


# ── fakes ────────────────────────────────────────────────────────────────────


def _role(
    rid, *, name="role", color=0, position=1, secondary=None, tertiary=None,
    icon=None, perms=None,
):
    r = MagicMock()
    r.id = rid
    r.name = name
    r.color = discord.Color(color)
    r.position = position
    r.secondary_color = secondary
    # Explicit so the reconcile's getattr diff sees a real None, not a
    # MagicMock auto-child (which would read as "changed" every pass).
    r.tertiary_color = tertiary
    r.display_icon = icon
    r.permissions = perms if perms is not None else discord.Permissions.none()
    r.edit = AsyncMock()
    r.delete = AsyncMock()
    return r


def _member(uid=USER_ID, *, display_name="Ziggy", roles=None):
    m = MagicMock()
    m.id = uid
    m.display_name = display_name
    m.roles = list(roles) if roles else []
    m.add_roles = AsyncMock()
    return m


def _guild(*, roles=None, member=None, features=(), me_top=50, channel=None):
    roles = list(roles) if roles else []
    g = MagicMock()
    g.id = GUILD_ID
    g.roles = roles
    g.features = list(features)
    g.get_role = lambda rid: next((r for r in roles if r.id == rid), None)
    g.get_member = lambda uid: member if member and member.id == uid else None
    me = MagicMock()
    me.top_role = MagicMock()
    me.top_role.position = me_top
    g.me = me
    g.create_role = AsyncMock()
    g.edit_role_positions = AsyncMock()
    g.get_channel = lambda cid: channel
    return g


def _bot(guild):
    b = MagicMock()
    b.get_guild = lambda gid: guild if gid == GUILD_ID else None
    return b


# ── color maths ─────────────────────────────────────────────────────────────


def test_parse_hex_color_variants():
    assert parse_hex_color("#7B2FF7") == 0x7B2FF7
    assert parse_hex_color("7b2ff7") == 0x7B2FF7
    assert parse_hex_color("  #FFFFFF ") == 0xFFFFFF
    assert parse_hex_color("nope") is None
    assert parse_hex_color("#FFF") is None  # 3-digit shorthand not accepted
    assert parse_hex_color("#GGGGGG") is None


def test_delta_e_identity_is_zero():
    assert delta_e_cie76((120, 47, 247), (120, 47, 247)) == pytest.approx(0.0)


def test_delta_e_black_white_is_large():
    # Black↔white is the maximum lightness difference — comfortably over 25.
    assert delta_e_cie76((0, 0, 0), (255, 255, 255)) > 90


def test_find_color_clash_flags_near_staff_color():
    admin = _role(
        1, name="Admins", color=0xFF0000,
        perms=discord.Permissions(administrator=True),
    )
    guild = _guild(roles=[admin])
    # A near-identical red clashes; a distant blue does not.
    assert find_color_clash(guild, 0xFE0101) is admin
    assert find_color_clash(guild, 0x0000FF) is None


def test_find_color_clash_ignores_non_staff_and_default_color():
    plain = _role(1, name="Member", color=0xFF0000)  # colorful but not staff
    colorless_staff = _role(
        2, name="Mods", color=0x000000,
        perms=discord.Permissions(manage_guild=True),
    )
    guild = _guild(roles=[plain, colorless_staff])
    assert find_color_clash(guild, 0xFF0000) is None


# ── feature gate ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_gate_ok_matrix():
    guild = _guild(features=("ROLE_ICONS",))
    bot = _bot(guild)
    assert await feature_gate_ok(bot, GUILD_ID, "role_icon") is True
    assert await feature_gate_ok(bot, GUILD_ID, "role_gradient") is False
    assert await feature_gate_ok(bot, GUILD_ID, "role_holographic") is False
    assert await feature_gate_ok(bot, GUILD_ID, "role_color") is True  # un-gated
    # Missing guild → cannot confirm support.
    assert await feature_gate_ok(MagicMock(get_guild=lambda g: None), GUILD_ID, "role_icon") is False


# ── projection matrix ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_creates_solid_color_role(db):
    _add_rental(db, "role_color")
    _set_desired(db, color=0x7B2FF7)
    new_role = _role(999, position=5)
    member = _member()
    guild = _guild(member=member)
    guild.create_role.return_value = new_role

    ok = await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    assert ok is True
    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["color"] == discord.Color(0x7B2FF7)
    assert "secondary_color" not in kwargs  # solid, not gradient
    member.add_roles.assert_awaited_once()
    # role_id persisted for the next projection.
    with open_db(db) as conn:
        row = get_personal_role(conn, GUILD_ID, USER_ID)
    assert row is not None and row["role_id"] == 999


@pytest.mark.asyncio
async def test_apply_gradient_supersedes_solid(db):
    _add_rental(db, "role_color")
    _add_rental(db, "role_gradient")
    _set_desired(db, color=0x111111, color2=0x222222)
    guild = _guild(member=_member(), features=("ENHANCED_ROLE_COLORS",))
    guild.create_role.return_value = _role(999)

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["color"] == discord.Color(0x111111)
    assert kwargs["secondary_color"] == discord.Color(0x222222)


@pytest.mark.asyncio
async def test_apply_gradient_without_feature_falls_back_to_solid(db):
    _add_rental(db, "role_color")
    _add_rental(db, "role_gradient")
    _set_desired(db, color=0x111111, color2=0x222222)
    guild = _guild(member=_member(), features=())  # no ENHANCED_ROLE_COLORS
    guild.create_role.return_value = _role(999)

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["color"] == discord.Color(0x111111)
    assert "secondary_color" not in kwargs


# Discord's one accepted (primary, secondary, tertiary) holographic triple.
_HOLO = (11127295, 16759788, 16761760)


@pytest.mark.asyncio
async def test_apply_creates_holographic_preset(db):
    # Renting holographic wears Discord's fixed preset — the member's stored
    # colours are irrelevant; all three preset colours are set on create.
    _add_rental(db, "role_holographic")
    _set_desired(db, color=0xABCDEF, color2=0x123456)  # ignored by holographic
    guild = _guild(member=_member(), features=("ENHANCED_ROLE_COLORS",))
    guild.create_role.return_value = _role(999)

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    kwargs = guild.create_role.await_args.kwargs
    p, s, t = _HOLO
    assert kwargs["color"] == discord.Color(p)
    assert kwargs["secondary_color"] == discord.Color(s)
    assert kwargs["tertiary_color"] == discord.Color(t)


@pytest.mark.asyncio
async def test_apply_holographic_supersedes_gradient(db):
    # Holding both, the shimmer wins — no stale two-colour fade.
    _add_rental(db, "role_gradient")
    _add_rental(db, "role_holographic")
    _set_desired(db, color=0x111111, color2=0x222222)
    guild = _guild(member=_member(), features=("ENHANCED_ROLE_COLORS",))
    guild.create_role.return_value = _role(999)

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["tertiary_color"] == discord.Color(_HOLO[2])


@pytest.mark.asyncio
async def test_apply_holographic_without_feature_is_inert(db):
    # No ENHANCED_ROLE_COLORS ⇒ holographic is dropped; with nothing else
    # entitled the role projects with the default colour and no extra colours.
    _add_rental(db, "role_holographic")
    _set_desired(db, color=0x111111)
    guild = _guild(member=_member(), features=())
    guild.create_role.return_value = _role(999)

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["color"] == discord.Color.default()
    assert "secondary_color" not in kwargs
    assert "tertiary_color" not in kwargs


@pytest.mark.asyncio
async def test_apply_downgrade_from_holographic_clears_tertiary(db):
    # Holographic lapses to solid colour ⇒ the tertiary/secondary are cleared.
    _add_rental(db, "role_color")  # only solid colour remains
    _set_desired(db, color=0x123456, role_id=999)
    existing = _role(
        999, name="Ziggy", color=0x123456, position=11,
        secondary=discord.Color(_HOLO[1]), tertiary=discord.Color(_HOLO[2]),
    )
    guild = _guild(roles=[existing], member=_member(roles=[existing]))

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    edits = existing.edit.await_args.kwargs
    assert edits["secondary_color"] is None
    assert edits["tertiary_color"] is None


@pytest.mark.asyncio
async def test_apply_name_and_color(db):
    _add_rental(db, "role_name")
    _add_rental(db, "role_color")
    _set_desired(db, name="Stardust", color=0x00FF00)
    guild = _guild(member=_member())
    guild.create_role.return_value = _role(999)

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["name"] == "Stardust"
    assert kwargs["color"] == discord.Color(0x00FF00)


@pytest.mark.asyncio
async def test_apply_name_defaults_to_display_name_without_name_perk(db):
    _add_rental(db, "role_color")  # color only, no role_name
    _set_desired(db, name="ShouldBeIgnored", color=0x00FF00)
    guild = _guild(member=_member(display_name="Ziggy"))
    guild.create_role.return_value = _role(999)

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    assert guild.create_role.await_args.kwargs["name"] == "Ziggy"


@pytest.mark.asyncio
async def test_apply_positions_above_cosmetics_anchor(db):
    _add_rental(db, "role_color")
    _set_desired(db, color=0x123456)
    anchor = _role(50, name="#### Cosmetics", position=10)
    new_role = _role(999, position=1)
    guild = _guild(roles=[anchor], member=_member(), me_top=40)
    guild.create_role.return_value = new_role

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    positions = guild.edit_role_positions.await_args.kwargs["positions"]
    assert positions[new_role] == 11  # anchor.position + 1, under the bot's top role


@pytest.mark.asyncio
async def test_apply_downgrade_clears_secondary_and_icon(db):
    """Gradient+icon lapse to color-only ⇒ the role's extras are cleared."""
    _add_rental(db, "role_color")  # only color remains entitled
    _set_desired(db, color=0x123456)
    existing = _role(
        999, name="Ziggy", color=0x123456, position=11,
        secondary=discord.Color(0x999999), icon=MagicMock(),
    )
    _set_desired(db, role_id=999)
    guild = _guild(roles=[existing], member=_member(roles=[existing]))

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    edits = existing.edit.await_args.kwargs
    assert edits["secondary_color"] is None
    assert edits["display_icon"] is None


@pytest.mark.asyncio
async def test_apply_switches_icon_when_desired_changes(db):
    """A different desired icon re-uploads even though the role already has one.

    Regression guard: the reconcile diffs the icon by presence only, so without
    the projected-icon tracking a catalog switch (both states "have an icon")
    would emit no edit and keep the stale icon.
    """
    _add_rental(db, "role_icon")
    _set_desired(db, role_id=999, icon_path="/icons/b", projected_icon_path="/icons/a")
    existing = _role(999, name="Ziggy", icon=MagicMock())  # already wears an icon
    guild = _guild(
        roles=[existing], member=_member(roles=[existing]), features=("ROLE_ICONS",)
    )

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    edits = existing.edit.await_args.kwargs
    assert edits["display_icon"] == "/icons/b"  # a non-file spec projects as-is
    with open_db(db) as conn:
        row = get_personal_role(conn, GUILD_ID, USER_ID)
    assert row["projected_icon_path"] == "/icons/b"  # advanced for the next switch


@pytest.mark.asyncio
async def test_apply_does_not_reupload_unchanged_icon(db):
    """Same desired icon already projected ⇒ steady state, no re-upload."""
    _add_rental(db, "role_icon")
    _set_desired(db, role_id=999, icon_path="/icons/a", projected_icon_path="/icons/a")
    existing = _role(999, name="Ziggy", icon=MagicMock())
    guild = _guild(
        roles=[existing], member=_member(roles=[existing]), features=("ROLE_ICONS",)
    )

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    existing.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_is_noop_when_role_already_matches(db):
    _add_rental(db, "role_color")
    _set_desired(db, color=0x123456, role_id=999)
    existing = _role(999, name="Ziggy", color=0x123456, position=11)
    guild = _guild(roles=[existing], member=_member(roles=[existing]))

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    existing.edit.assert_not_awaited()  # steady state costs no edits


@pytest.mark.asyncio
async def test_apply_role_ceiling_alert(db):
    _add_rental(db, "role_color")
    _set_desired(db, color=0x123456)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    many_roles = [_role(i, position=i) for i in range(205)]
    guild = _guild(roles=many_roles, member=_member(), channel=channel)
    guild.create_role.return_value = _role(999)
    # Point the alert at the bank channel.
    from bot_modules.services.economy_service import save_econ_settings

    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"bank_channel_id": 12345})

    await apply_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    channel.send.assert_awaited_once()
    assert "250" in channel.send.await_args.args[0]


# ── revoke ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_deletes_role_and_row_when_no_entitlements(db):
    _set_desired(db, color=0x123456, role_id=999)  # desired row, but no rentals
    existing = _role(999)
    guild = _guild(roles=[existing], member=_member())

    await revoke_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    existing.delete.assert_awaited_once()
    with open_db(db) as conn:
        assert get_personal_role(conn, GUILD_ID, USER_ID) is None


@pytest.mark.asyncio
async def test_revoke_reprojects_when_entitlements_remain(db):
    _add_rental(db, "role_color")  # color survives a gradient lapse
    _set_desired(db, color=0x123456, role_id=999)
    existing = _role(999, name="Ziggy", color=0x123456, position=11)
    guild = _guild(roles=[existing], member=_member(roles=[existing]))

    await revoke_role_perks(_bot(guild), db, GUILD_ID, USER_ID)

    existing.delete.assert_not_awaited()  # re-projected, not deleted
    with open_db(db) as conn:
        assert get_personal_role(conn, GUILD_ID, USER_ID) is not None


# ── should_revert_nick: lapsed name perk resets the nick it set (#56 follow-up) ─


def test_should_revert_nick_when_name_perk_lapsed_and_nick_matches():
    # role_name gone from entitlements, nick still equals the perk's name.
    assert should_revert_nick(set(), "Sir Fluffy", "Sir Fluffy") is True
    # other perks can remain — a role_name-only lapse still reverts.
    assert should_revert_nick({"role_color"}, "Sir Fluffy", "Sir Fluffy") is True


def test_should_not_revert_nick_when_still_entitled_or_nick_changed():
    # still holds role_name → keep the nick.
    assert should_revert_nick({"role_name"}, "Sir Fluffy", "Sir Fluffy") is False
    # member changed their nick since (e.g. a game name-penalty) → don't clobber.
    assert should_revert_nick(set(), "Sir Fluffy", "Jailed Loser") is False
    # no nick / no stored name → nothing to revert.
    assert should_revert_nick(set(), "Sir Fluffy", None) is False
    assert should_revert_nick(set(), "", "Sir Fluffy") is False
