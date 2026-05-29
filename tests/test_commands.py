"""Tests for slash command handlers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.commands.role_grant_commands import register_role_grant_commands


# ── Helpers ───────────────────────────────────────────────────────────

class _CommandCapture:
    """Captures slash command callbacks registered via bot.tree.command."""

    def __init__(self):
        self.commands: dict[str, Any] = {}
        self.error_handler = None
        bot = MagicMock()
        bot.tree.command = self._capture_command
        bot.tree.error = self._capture_error
        self.bot = bot

    def _capture_command(self, name: str, **kwargs):
        def decorator(fn):
            self.commands[name] = fn
            return fn
        return decorator

    def _capture_error(self, fn):
        self.error_handler = fn
        return fn

    def get(self, name: str):
        return self.commands[name]


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
    cap = _CommandCapture()
    ctx = _make_ctx(can_grant_any_role=True, denizen_role_id=999)
    register_role_grant_commands(cap.bot, ctx)
    cmd = cap.get("grant")

    async def grant(interaction, member):
        return await cmd(interaction, "denizen", member)

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
