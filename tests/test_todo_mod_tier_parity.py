"""The todo list's two surfaces must mean the same thing by "moderator".

The bug this pins: a moderator with Timeout Members / Manage Messages but no
Manage Server passed the dashboard's ``moderator`` tier and was refused by the
chore board's buttons ("Only moderators can tick off chores"), because the
board gated on the *games* cogs' administrator/manage_guild/manage_channels
rule instead. Both surfaces now resolve a moderator the same way — the guild's
configured ``mod_role_ids``/``admin_role_ids``, with Discord's
administrator/manage_guild short-circuit.

Kept as one file because the contract *is* the agreement between the two
layers; splitting it per surface is how they drifted apart in the first place.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import open_db, set_config_value as _db_set
from tests.db_template import migrated_db
from web_server.auth import resolve_guild_perms

GUILD = 1469491362444480666
MOD_ROLE = 1469783611229339912
ADMIN_ROLE = 1470164416224956558

# Discord permission bits, named so the cases read like the Discord UI.
ADMINISTRATOR = 0x8
MANAGE_GUILD = 0x20
MANAGE_CHANNELS = 0x10
MANAGE_MESSAGES = 0x2000
KICK_MEMBERS = 0x2
MODERATE_MEMBERS = 0x0000010000000000


def _ctx(tmp_path) -> AppContext:
    db_path = tmp_path / "todo_perms.db"
    migrated_db(db_path)
    ctx = AppContext(
        bot=MagicMock(),
        log=logging.getLogger("test"),
        db_path=db_path,
        guild_id=GUILD,
        debug=True,
    )
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", str(MOD_ROLE), guild_id=GUILD)
        _db_set(conn, "admin_role_ids", str(ADMIN_ROLE), guild_id=GUILD)
    return ctx


def _discord_is_mod(ctx: AppContext, *, bits: int, role_ids: tuple[int, ...]) -> bool:
    """What the todo board's buttons and ``/todo`` decide, via ctx.is_mod."""
    member = MagicMock()
    member.roles = [MagicMock(id=rid) for rid in role_ids]
    ix = MagicMock(spec=discord.Interaction)
    ix.guild_id = GUILD
    ix.permissions = MagicMock(
        administrator=bool(bits & ADMINISTRATOR),
        manage_guild=bool(bits & MANAGE_GUILD),
    )
    ix.guild = MagicMock()
    ix.guild.get_member = MagicMock(return_value=member)
    ix.user = member
    return ctx.is_mod(ix)


def _web_is_mod(ctx: AppContext, *, bits: int, role_ids: tuple[int, ...]) -> bool:
    """What the dashboard's ``require_perms({"moderator"})`` decides."""
    cfg = ctx.guild_config(GUILD)
    return "moderator" in resolve_guild_perms(
        bits,
        role_ids=role_ids,
        mod_role_ids=cfg.mod_role_ids,
        admin_role_ids=cfg.admin_role_ids,
    )


@pytest.mark.parametrize(
    "label,bits,role_ids,expected",
    [
        pytest.param(
            "the mod who was refused today: mod role, no elevated bits",
            MANAGE_MESSAGES | KICK_MEMBERS | MODERATE_MEMBERS,
            (MOD_ROLE,),
            True,
            id="mod_role_without_manage_guild",
        ),
        pytest.param(
            "mod role and nothing else at all",
            0,
            (MOD_ROLE,),
            True,
            id="mod_role_bare",
        ),
        pytest.param(
            "the admin role counts as mod too",
            0,
            (ADMIN_ROLE,),
            True,
            id="admin_role",
        ),
        pytest.param(
            "moderation bits but not on the mod team",
            MANAGE_MESSAGES | KICK_MEMBERS | MODERATE_MEMBERS,
            (999,),
            False,
            id="bits_without_role",
        ),
        pytest.param(
            "Manage Channels opened the board and granted nothing on the web",
            MANAGE_CHANNELS,
            (999,),
            False,
            id="manage_channels_only",
        ),
        pytest.param(
            "Manage Server short-circuits on both surfaces",
            MANAGE_GUILD,
            (999,),
            True,
            id="manage_guild",
        ),
        pytest.param(
            "administrator short-circuits on both surfaces",
            ADMINISTRATOR,
            (),
            True,
            id="administrator",
        ),
        pytest.param(
            "an ordinary member",
            0,
            (999,),
            False,
            id="plain_member",
        ),
    ],
)
def test_both_surfaces_agree_on_moderator(tmp_path, label, bits, role_ids, expected):
    ctx = _ctx(tmp_path)
    web = _web_is_mod(ctx, bits=bits, role_ids=role_ids)
    discord_side = _discord_is_mod(ctx, bits=bits, role_ids=role_ids)
    assert web is expected, f"dashboard disagrees — {label}"
    assert discord_side is expected, f"Discord board disagrees — {label}"


def test_unconfigured_guild_falls_back_to_permission_bits(tmp_path):
    """A guild with no staff roles configured must not lock itself out.

    With nothing to read, "moderator" stays the old bit rule rather than
    collapsing to administrator-only — otherwise adding this feature would
    silently revoke the dashboard from every mod of a guild that never filled
    the roles in.
    """
    assert "moderator" in resolve_guild_perms(
        MANAGE_MESSAGES, role_ids=(999,), mod_role_ids=(), admin_role_ids=()
    )


def test_admin_tier_is_not_widened_by_a_config_row(tmp_path):
    """Holding the configured admin role is mod, not dashboard admin.

    Board placement is admin-gated, and admin stays Discord's ADMINISTRATOR
    bit: a config row anyone with dashboard access can edit must not be a path
    to granting oneself the ceiling.
    """
    perms = resolve_guild_perms(
        0,
        role_ids=(ADMIN_ROLE,),
        mod_role_ids=(MOD_ROLE,),
        admin_role_ids=(ADMIN_ROLE,),
    )
    assert "moderator" in perms
    assert "admin" not in perms
    assert "manage_server" not in perms
