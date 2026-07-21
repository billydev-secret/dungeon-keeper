"""Tests for slash command handlers.

The /grant command lives in ``bot_modules.cogs.role_grant_cog`` (RoleGrantCog),
which resolves the grant config and delegates to
``bot_modules.commands.role_grant_commands._execute_grant``. Tests drive the
cog's command callback directly so both the permission/config gate and the
shared execution path are covered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs.role_grant_cog import RoleGrantCog


# ── Helpers ───────────────────────────────────────────────────────────


def _make_interaction(*, user_id: int = 100, guild: Any = None, channel: Any = None) -> MagicMock:
    ix = MagicMock()
    ix.response.send_message = AsyncMock()
    ix.response.is_done = MagicMock(return_value=False)
    ix.response.defer = AsyncMock()
    ix.followup.send = AsyncMock()
    user = MagicMock()
    user.id = user_id
    ix.user = user
    ix.guild = guild
    ix.channel = channel
    ix.guild_id = guild.id if guild else None
    return ix


def _make_ctx(**kwargs) -> MagicMock:
    ctx = MagicMock()
    ctx.is_mod = MagicMock(return_value=kwargs.get("is_mod", False))
    ctx.can_grant_any_role = MagicMock(return_value=kwargs.get("can_grant_any_role", False))
    ctx.can_use_xp_grant = MagicMock(return_value=kwargs.get("can_use_xp_grant", False))
    actor = MagicMock()
    actor.id = kwargs.get("actor_id", 100)
    ctx.get_interaction_member = MagicMock(return_value=actor)
    ctx.grant_roles = kwargs.get("grant_roles", {
        "denizen": {"label": "Denizen", "role_id": kwargs.get("denizen_role_id", 0), "log_channel_id": 0, "announce_channel_id": 0, "grant_message": "", "required_role_id": 0},
        "nsfw": {"label": "NSFW", "role_id": 0, "log_channel_id": 0, "announce_channel_id": 0, "grant_message": "", "required_role_id": 0},
        "veteran": {"label": "Veteran", "role_id": 0, "log_channel_id": 0, "announce_channel_id": 0, "grant_message": "", "required_role_id": 0},
        "kink": {"label": "Kink", "role_id": 0, "log_channel_id": 0, "announce_channel_id": 0, "grant_message": "", "required_role_id": 0},
        "goldengirl": {"label": "Golden Girl", "role_id": 0, "log_channel_id": 0, "announce_channel_id": 0, "grant_message": "", "required_role_id": 0},
    })
    # Grant definitions are read per-guild via ctx.guild_config(gid).grant_roles.
    _gc = MagicMock()
    _gc.grant_roles = ctx.grant_roles
    ctx.guild_config = MagicMock(return_value=_gc)
    ctx.can_use_grant_role = MagicMock(return_value=kwargs.get("can_grant_any_role", False))
    ctx.greeter_role_id = kwargs.get("greeter_role_id", 0)
    ctx.spoiler_required_channels = kwargs.get("spoiler_required_channels", set())
    ctx.xp_excluded_channel_ids = kwargs.get("xp_excluded_channel_ids", set())
    ctx.xp_grant_allowed_user_ids = kwargs.get("xp_grant_allowed_user_ids", set())
    ctx.get_xp_config_target_channel = MagicMock(return_value=kwargs.get("target_channel"))
    ctx.add_config_id_value = MagicMock(return_value=set())
    ctx.remove_config_id_value = MagicMock(return_value=set())
    ctx.set_config_value = MagicMock(return_value="0")
    ctx.open_db = MagicMock()
    ctx.open_db.return_value.__enter__ = MagicMock(return_value=MagicMock())
    ctx.open_db.return_value.__exit__ = MagicMock(return_value=False)
    return ctx


class _MockRole:
    def __init__(self, position: int = 0, role_id: int = 1, name: str = "Role"):
        self.position = position
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"

    def __ge__(self, other: _MockRole) -> bool:
        return self.position >= other.position

    def __lt__(self, other: _MockRole) -> bool:
        return self.position < other.position


def _make_member(*, bot: bool = False, user_id: int = 200, roles=None) -> MagicMock:
    m = MagicMock()
    m.bot = bot
    m.id = user_id
    m.roles = roles or []
    m.mention = f"<@{user_id}>"
    m.add_roles = AsyncMock()
    return m


def _guild_with_role(role):
    guild = MagicMock()
    guild.get_role = MagicMock(return_value=role)
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = _MockRole(position=10)
    return guild


# ── grant command tests ───────────────────────────────────────────────

@pytest.fixture
def grant_setup():
    ctx = _make_ctx(can_grant_any_role=True, denizen_role_id=999)
    cog = RoleGrantCog(MagicMock(), ctx)
    cmd = RoleGrantCog.grant_cmd.callback

    async def grant(interaction, member):
        return await cmd(cog, interaction, "denizen", member)

    return ctx, grant


async def test_no_permission_denied(grant_setup):
    ctx, grant = grant_setup
    ctx.can_use_grant_role.return_value = False
    ix = _make_interaction()
    await grant(ix, _make_member())
    ix.response.send_message.assert_awaited_once()
    assert "permission" in ix.response.send_message.call_args[0][0].lower()
    assert ix.response.send_message.call_args[1]["ephemeral"] is True


async def test_bot_target_denied(grant_setup):
    _, grant = grant_setup
    ix = _make_interaction(guild=MagicMock())
    await grant(ix, _make_member(bot=True))
    assert "bots" in ix.response.send_message.call_args[0][0].lower()


async def test_self_assign_denied_for_non_mod(grant_setup):
    ctx, grant = grant_setup
    ctx.is_mod.return_value = False
    ctx.get_interaction_member.return_value.id = 200
    ix = _make_interaction(user_id=200, guild=MagicMock())
    await grant(ix, _make_member(user_id=200))
    assert "yourself" in ix.response.send_message.call_args[0][0].lower()


async def test_self_assign_allowed_for_mod(grant_setup):
    ctx, grant = grant_setup
    ctx.is_mod.return_value = True
    ctx.get_interaction_member.return_value.id = 200
    denizen_role = _MockRole(position=1, role_id=999)
    guild = _guild_with_role(denizen_role)
    ix = _make_interaction(user_id=200, guild=guild)
    member = _make_member(user_id=200)
    await grant(ix, member)
    member.add_roles.assert_awaited_once()


async def test_role_not_configured_denied(grant_setup):
    ctx, grant = grant_setup
    ctx.grant_roles["denizen"]["role_id"] = 0
    ix = _make_interaction(guild=MagicMock())
    await grant(ix, _make_member())
    assert "not configured" in ix.response.send_message.call_args[0][0].lower()


async def test_role_not_found_denied(grant_setup):
    _, grant = grant_setup
    guild = MagicMock()
    guild.get_role = MagicMock(return_value=None)
    ix = _make_interaction(guild=guild)
    await grant(ix, _make_member())
    assert "no longer exists" in ix.response.send_message.call_args[0][0].lower()


async def test_member_already_has_role_denied(grant_setup):
    _, grant = grant_setup
    denizen_role = _MockRole(position=1, role_id=999)
    ix = _make_interaction(guild=_guild_with_role(denizen_role))
    await grant(ix, _make_member(roles=[denizen_role]))
    assert "already has" in ix.response.send_message.call_args[0][0].lower()


async def test_bot_missing_manage_roles_denied(grant_setup):
    _, grant = grant_setup
    denizen_role = _MockRole(position=1, role_id=999)
    guild = _guild_with_role(denizen_role)
    guild.me.guild_permissions.manage_roles = False
    ix = _make_interaction(guild=guild)
    await grant(ix, _make_member())
    assert "manage roles" in ix.response.send_message.call_args[0][0].lower()


async def test_role_above_bot_denied(grant_setup):
    _, grant = grant_setup
    denizen_role = _MockRole(position=10, role_id=999)
    guild = MagicMock()
    guild.get_role = MagicMock(return_value=denizen_role)
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = _MockRole(position=5)
    ix = _make_interaction(guild=guild)
    await grant(ix, _make_member())
    assert "above my highest role" in ix.response.send_message.call_args[0][0].lower()


async def test_forbidden_on_add_roles_handled(grant_setup):
    _, grant = grant_setup
    denizen_role = _MockRole(position=1, role_id=999)
    guild = _guild_with_role(denizen_role)
    ix = _make_interaction(guild=guild)
    member = _make_member()
    forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Permissions")
    member.add_roles = AsyncMock(side_effect=forbidden)
    await grant(ix, member)
    ix.response.defer.assert_awaited_once()
    assert "couldn't grant" in ix.followup.send.call_args[0][0].lower()


async def test_grant_success_posts_public_message(grant_setup):
    _, grant = grant_setup
    denizen_role = _MockRole(position=1, role_id=999)
    guild = _guild_with_role(denizen_role)
    ix = _make_interaction(guild=guild)
    member = _make_member()
    await grant(ix, member)
    member.add_roles.assert_awaited_once()
    ix.response.defer.assert_awaited_once()
    ix.followup.send.assert_awaited_once()
    assert "granted" in ix.followup.send.call_args[0][0].lower()


# ── /grant_missing command tests ────────────────────────────────────────

GM_GUILD_ID = 12345
GM_NSFW_ROLE_ID = 555


def _seed_level(db_path, *, user_id: int, level: int, guild_id: int = GM_GUILD_ID):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO member_xp (guild_id, user_id, total_xp, level, announced_level) "
        "VALUES (?, ?, 0, ?, ?)",
        (guild_id, user_id, level, level),
    )
    conn.commit()
    conn.close()


def _seed_inactive(db_path, *, user_id: int, guild_id: int = GM_GUILD_ID):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO inactive_members (guild_id, user_id, stored_roles, created_at, status) "
        "VALUES (?, ?, '[]', 0, 'active')",
        (guild_id, user_id),
    )
    conn.commit()
    conn.close()


def _seed_jail(db_path, *, user_id: int, guild_id: int = GM_GUILD_ID):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO jails (guild_id, user_id, moderator_id, stored_roles, created_at, status) "
        "VALUES (?, ?, 0, '[]', 0, 'active')",
        (guild_id, user_id),
    )
    conn.commit()
    conn.close()


def _seed_prune_rule(
    db_path, *, role_id: int, inactivity_days: int = 30, guild_id: int = GM_GUILD_ID
):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO inactivity_prune_rules (guild_id, role_id, inactivity_days) VALUES (?, ?, ?)",
        (guild_id, role_id, inactivity_days),
    )
    conn.commit()
    conn.close()


def _seed_prune_exception(db_path, *, user_id: int, guild_id: int = GM_GUILD_ID):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO inactivity_prune_exceptions (guild_id, user_id) VALUES (?, ?)",
        (guild_id, user_id),
    )
    conn.commit()
    conn.close()


def _seed_activity(db_path, *, user_id: int, days_ago: float, guild_id: int = GM_GUILD_ID):
    import sqlite3
    import time

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO member_activity (guild_id, user_id, last_channel_id, last_message_id, last_message_at) "
        "VALUES (?, ?, 1, 1, ?)",
        (guild_id, user_id, time.time() - days_ago * 86400),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def grant_missing_setup(tmp_path):
    from bot_modules.core.db_utils import open_db
    from migrations import apply_migrations_sync

    db_path = tmp_path / "gm.db"
    apply_migrations_sync(db_path)

    ctx = _make_ctx(can_grant_any_role=True, is_mod=True)
    ctx.grant_roles["nsfw"]["role_id"] = GM_NSFW_ROLE_ID
    ctx.open_db = lambda: open_db(db_path)
    cog = RoleGrantCog(MagicMock(), ctx)
    cmd = RoleGrantCog.grant_missing_cmd.callback

    async def grant_missing(interaction, role="nsfw", min_level=5):
        return await cmd(cog, interaction, role, min_level)

    return ctx, grant_missing, db_path


def _gm_guild(members: dict[int, Any]):
    nsfw_role = _MockRole(role_id=GM_NSFW_ROLE_ID)
    guild = MagicMock()
    guild.id = GM_GUILD_ID
    guild.get_role = MagicMock(return_value=nsfw_role)
    guild.get_member = MagicMock(side_effect=lambda uid: members.get(uid))
    return guild, nsfw_role


async def test_grant_missing_denied_for_non_mod(grant_missing_setup):
    ctx, grant_missing, _ = grant_missing_setup
    ctx.is_mod.return_value = False
    ix = _make_interaction(guild=_gm_guild({})[0])
    await grant_missing(ix)
    assert "permission" in ix.response.send_message.call_args[0][0].lower()


async def test_grant_missing_role_not_configured_denied(grant_missing_setup):
    ctx, grant_missing, _ = grant_missing_setup
    ctx.grant_roles["nsfw"]["role_id"] = 0
    ix = _make_interaction(guild=_gm_guild({})[0])
    await grant_missing(ix)
    assert "not configured" in ix.response.send_message.call_args[0][0].lower()


async def test_grant_missing_lists_qualifying_member(grant_missing_setup):
    ctx, grant_missing, db_path = grant_missing_setup
    _seed_level(db_path, user_id=201, level=7)
    member = _make_member(user_id=201)
    guild, _ = _gm_guild({201: member})
    ix = _make_interaction(guild=guild)

    await grant_missing(ix)

    ix.response.defer.assert_awaited_once()
    embed = ix.followup.send.call_args[1]["embed"]
    assert "level 7" in embed.description.lower()
    assert member.mention in embed.description


async def test_grant_missing_excludes_members_who_already_have_the_role(grant_missing_setup):
    ctx, grant_missing, db_path = grant_missing_setup
    _seed_level(db_path, user_id=202, level=6)
    guild, nsfw_role = _gm_guild({})
    member = _make_member(user_id=202, roles=[nsfw_role])
    guild.get_member = MagicMock(side_effect=lambda uid: {202: member}.get(uid))
    ix = _make_interaction(guild=guild)

    await grant_missing(ix)

    assert "nobody at level" in ix.followup.send.call_args[0][0].lower()


async def test_grant_missing_excludes_inactive_members(grant_missing_setup):
    ctx, grant_missing, db_path = grant_missing_setup
    _seed_level(db_path, user_id=203, level=8)
    _seed_inactive(db_path, user_id=203)
    member = _make_member(user_id=203)
    guild, _ = _gm_guild({203: member})
    ix = _make_interaction(guild=guild)

    await grant_missing(ix)

    assert "nobody at level" in ix.followup.send.call_args[0][0].lower()


async def test_grant_missing_excludes_jailed_members(grant_missing_setup):
    ctx, grant_missing, db_path = grant_missing_setup
    _seed_level(db_path, user_id=205, level=9)
    _seed_jail(db_path, user_id=205)
    member = _make_member(user_id=205)
    guild, _ = _gm_guild({205: member})
    ix = _make_interaction(guild=guild)

    await grant_missing(ix)

    assert "nobody at level" in ix.followup.send.call_args[0][0].lower()


async def test_grant_missing_excludes_members_with_live_hold_role_but_no_db_row(
    grant_missing_setup,
):
    """A mod who strips roles by hand (skipping /inactive mark or /jail) never

    creates a DB row — the member's *current* Inactive role must still count.
    """
    import sqlite3

    from bot_modules.core.db_utils import set_config_value

    ctx, grant_missing, db_path = grant_missing_setup
    _seed_level(db_path, user_id=206, level=9)

    inactive_role_id = 777
    conn = sqlite3.connect(db_path)
    set_config_value(conn, "inactive_role_id", str(inactive_role_id), GM_GUILD_ID)
    conn.commit()
    conn.close()

    inactive_role = _MockRole(role_id=inactive_role_id)
    member = _make_member(user_id=206, roles=[inactive_role])
    guild, _ = _gm_guild({206: member})
    ix = _make_interaction(guild=guild)

    await grant_missing(ix)

    assert "nobody at level" in ix.followup.send.call_args[0][0].lower()


async def test_grant_missing_excludes_members_auto_pruned_for_inactivity(
    grant_missing_setup,
):
    """The inactivity-prune loop (30d default) auto-removes a configured role

    from long-inactive members — a plain one-way removal with no hold row
    anywhere. If it's *this* grant role, don't flag its own victims as missing.
    """
    ctx, grant_missing, db_path = grant_missing_setup
    _seed_level(db_path, user_id=207, level=5)
    _seed_prune_rule(db_path, role_id=GM_NSFW_ROLE_ID, inactivity_days=30)
    _seed_activity(db_path, user_id=207, days_ago=40)
    member = _make_member(user_id=207)
    guild, _ = _gm_guild({207: member})
    ix = _make_interaction(guild=guild)

    await grant_missing(ix)

    assert "nobody at level" in ix.followup.send.call_args[0][0].lower()


async def test_grant_missing_still_lists_prune_exception_members(grant_missing_setup):
    """A member on the prune exception list is never auto-pruned, so being

    inactive and missing the role is a real gap worth a mod's attention.
    """
    ctx, grant_missing, db_path = grant_missing_setup
    _seed_level(db_path, user_id=208, level=5)
    _seed_prune_rule(db_path, role_id=GM_NSFW_ROLE_ID, inactivity_days=30)
    _seed_activity(db_path, user_id=208, days_ago=40)
    _seed_prune_exception(db_path, user_id=208)
    member = _make_member(user_id=208)
    guild, _ = _gm_guild({208: member})
    ix = _make_interaction(guild=guild)

    await grant_missing(ix)

    embed = ix.followup.send.call_args[1]["embed"]
    assert "level 5" in embed.description.lower()
    assert member.mention in embed.description


async def test_grant_missing_excludes_below_threshold(grant_missing_setup):
    ctx, grant_missing, db_path = grant_missing_setup
    _seed_level(db_path, user_id=204, level=4)
    member = _make_member(user_id=204)
    guild, _ = _gm_guild({204: member})
    ix = _make_interaction(guild=guild)

    await grant_missing(ix, min_level=5)

    assert "nobody at level" in ix.followup.send.call_args[0][0].lower()
