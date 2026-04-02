"""Tests for slash command handlers."""
from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord

from commands.denizen_commands import register_denizen_commands
from commands.mod_commands import register_mod_commands
from commands.xp_commands import register_xp_commands


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_interaction(
    *,
    user_id: int = 100,
    guild: Any = None,
    channel: Any = None,
) -> MagicMock:
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
    ctx.can_grant_denizen = MagicMock(return_value=kwargs.get("can_grant_denizen", False))
    ctx.can_use_xp_grant = MagicMock(return_value=kwargs.get("can_use_xp_grant", False))
    actor = MagicMock()
    actor.id = kwargs.get("actor_id", 100)
    ctx.get_interaction_member = MagicMock(return_value=actor)
    ctx.grant_roles = kwargs.get("grant_roles", {
        "denizen": {"label": "Denizen", "role_id": kwargs.get("denizen_role_id", 0),
                    "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
        "nsfw": {"label": "NSFW", "role_id": 0,
                 "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
        "veteran": {"label": "Veteran", "role_id": 0,
                    "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
        "kink": {"label": "Kink", "role_id": 0,
                 "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
        "goldengirl": {"label": "Golden Girl", "role_id": 0,
                       "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
    })
    ctx.can_use_grant_role = MagicMock(return_value=kwargs.get("can_grant_denizen", False))
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
    """Minimal role mock supporting position-based comparison."""

    def __init__(self, position: int = 0, role_id: int = 1, name: str = "Role"):
        self.position = position
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"

    def __ge__(self, other: "_MockRole") -> bool:
        return self.position >= other.position

    def __lt__(self, other: "_MockRole") -> bool:
        return self.position < other.position


def _make_member(*, bot: bool = False, user_id: int = 200, roles=None) -> MagicMock:
    m = MagicMock()
    m.bot = bot
    m.id = user_id
    m.roles = roles or []
    m.mention = f"<@{user_id}>"
    m.add_roles = AsyncMock()
    return m


# ---------------------------------------------------------------------------
# grant command tests
# ---------------------------------------------------------------------------


class GrantCommandTests(unittest.TestCase):
    def setUp(self):
        cap = _CommandCapture()
        self.ctx = _make_ctx(can_grant_denizen=True, denizen_role_id=999)
        register_denizen_commands(cap.bot, self.ctx)
        self._grant_cmd = cap.get("grant")

    def grant(self, interaction, member):
        return self._grant_cmd(interaction, "denizen", member)

    def _guild_with_role(self, role):
        guild = MagicMock()
        guild.get_role = MagicMock(return_value=role)
        guild.me = MagicMock()
        guild.me.guild_permissions.manage_roles = True
        guild.me.top_role = _MockRole(position=10)
        return guild

    def test_no_permission_denied(self):
        self.ctx.can_use_grant_role.return_value = False
        ix = _make_interaction()
        _run(self.grant(ix, _make_member()))
        ix.response.send_message.assert_awaited_once()
        self.assertIn("permission", ix.response.send_message.call_args[0][0].lower())
        self.assertTrue(ix.response.send_message.call_args[1]["ephemeral"])

    def test_bot_target_denied(self):
        ix = _make_interaction(guild=MagicMock())
        _run(self.grant(ix, _make_member(bot=True)))
        self.assertIn("bots", ix.response.send_message.call_args[0][0].lower())

    def test_self_assign_denied_for_non_mod(self):
        self.ctx.is_mod.return_value = False
        self.ctx.get_interaction_member.return_value.id = 200
        ix = _make_interaction(user_id=200, guild=MagicMock())
        _run(self.grant(ix, _make_member(user_id=200)))
        self.assertIn("yourself", ix.response.send_message.call_args[0][0].lower())

    def test_self_assign_allowed_for_mod(self):
        self.ctx.is_mod.return_value = True
        self.ctx.get_interaction_member.return_value.id = 200
        denizen_role = _MockRole(position=1, role_id=999)
        guild = self._guild_with_role(denizen_role)
        ix = _make_interaction(user_id=200, guild=guild)
        member = _make_member(user_id=200)
        _run(self.grant(ix, member))
        member.add_roles.assert_awaited_once()

    def test_role_not_configured_denied(self):
        self.ctx.grant_roles["denizen"]["role_id"] = 0
        ix = _make_interaction(guild=MagicMock())
        _run(self.grant(ix, _make_member()))
        self.assertIn("not configured", ix.response.send_message.call_args[0][0].lower())

    def test_role_not_found_denied(self):
        guild = MagicMock()
        guild.get_role = MagicMock(return_value=None)
        ix = _make_interaction(guild=guild)
        _run(self.grant(ix, _make_member()))
        self.assertIn("no longer exists", ix.response.send_message.call_args[0][0].lower())

    def test_member_already_has_role_denied(self):
        denizen_role = _MockRole(position=1, role_id=999)
        ix = _make_interaction(guild=self._guild_with_role(denizen_role))
        _run(self.grant(ix, _make_member(roles=[denizen_role])))
        self.assertIn("already has", ix.response.send_message.call_args[0][0].lower())

    def test_bot_missing_manage_roles_denied(self):
        denizen_role = _MockRole(position=1, role_id=999)
        guild = self._guild_with_role(denizen_role)
        guild.me.guild_permissions.manage_roles = False
        ix = _make_interaction(guild=guild)
        _run(self.grant(ix, _make_member()))
        self.assertIn("manage roles", ix.response.send_message.call_args[0][0].lower())

    def test_role_above_bot_denied(self):
        denizen_role = _MockRole(position=10, role_id=999)
        guild = MagicMock()
        guild.get_role = MagicMock(return_value=denizen_role)
        guild.me = MagicMock()
        guild.me.guild_permissions.manage_roles = True
        guild.me.top_role = _MockRole(position=5)
        ix = _make_interaction(guild=guild)
        _run(self.grant(ix, _make_member()))
        self.assertIn("above my highest role", ix.response.send_message.call_args[0][0].lower())

    def test_forbidden_on_add_roles_handled(self):
        denizen_role = _MockRole(position=1, role_id=999)
        guild = self._guild_with_role(denizen_role)
        ix = _make_interaction(guild=guild)
        member = _make_member()
        forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Permissions")
        member.add_roles = AsyncMock(side_effect=forbidden)
        _run(self.grant(ix, member))
        self.assertIn("couldn't grant", ix.response.send_message.call_args[0][0].lower())

    def test_success_posts_public_message(self):
        denizen_role = _MockRole(position=1, role_id=999)
        guild = self._guild_with_role(denizen_role)
        ix = _make_interaction(guild=guild)
        member = _make_member()
        _run(self.grant(ix, member))
        member.add_roles.assert_awaited_once()
        ix.response.send_message.assert_awaited_once()
        self.assertFalse(ix.response.send_message.call_args[1]["ephemeral"])
        self.assertIn("granted", ix.response.send_message.call_args[0][0].lower())



# ---------------------------------------------------------------------------
# XP command permission guard tests
# ---------------------------------------------------------------------------


class XpCommandPermissionTests(unittest.TestCase):
    def setUp(self):
        cap = _CommandCapture()
        self.ctx = _make_ctx()
        register_xp_commands(cap.bot, self.ctx)
        self.cap = cap

    def _cmd(self, name):
        return self.cap.get(name)

    def test_xp_give_no_permission_denied(self):
        self.ctx.can_use_xp_grant.return_value = False
        ix = _make_interaction()
        _run(self._cmd("xp_give")(ix, _make_member()))
        self.assertIn("permission", ix.response.send_message.call_args[0][0].lower())

    def test_xp_give_bot_target_denied(self):
        self.ctx.can_use_xp_grant.return_value = True
        ix = _make_interaction(guild=MagicMock())
        _run(self._cmd("xp_give")(ix, _make_member(bot=True)))
        self.assertIn("bots cannot", ix.response.send_message.call_args[0][0].lower())

    def test_xp_give_self_grant_denied(self):
        self.ctx.can_use_xp_grant.return_value = True
        ix = _make_interaction(user_id=200, guild=MagicMock())
        _run(self._cmd("xp_give")(ix, _make_member(user_id=200)))
        self.assertIn("yourself", ix.response.send_message.call_args[0][0].lower())

    def test_xp_give_no_guild_denied(self):
        self.ctx.can_use_xp_grant.return_value = True
        ix = _make_interaction(guild=None)
        _run(self._cmd("xp_give")(ix, _make_member()))
        self.assertIn("server", ix.response.send_message.call_args[0][0].lower())

    def test_xp_excluded_channels_non_mod_denied(self):
        self.ctx.is_mod.return_value = False
        ix = _make_interaction()
        _run(self._cmd("xp_excluded_channels")(ix))
        self.assertIn("permission", ix.response.send_message.call_args[0][0].lower())

    def test_xp_excluded_channels_empty(self):
        self.ctx.is_mod.return_value = True
        self.ctx.xp_excluded_channel_ids = set()
        ix = _make_interaction()
        _run(self._cmd("xp_excluded_channels")(ix))
        self.assertIn("all channels", ix.response.send_message.call_args[0][0].lower())

    def test_xp_leaderboards_no_guild_denied(self):
        ix = _make_interaction(guild=None)
        _run(self._cmd("xp_leaderboards")(ix))
        self.assertIn("server", ix.response.send_message.call_args[0][0].lower())

    def test_xp_backfill_non_mod_denied(self):
        self.ctx.is_mod.return_value = False
        ix = _make_interaction()
        _run(self._cmd("xp_backfill_history")(ix))
        self.assertIn("permission", ix.response.send_message.call_args[0][0].lower())


# ---------------------------------------------------------------------------
# XP config command success paths
# ---------------------------------------------------------------------------


class XpConfigCommandSuccessTests(unittest.TestCase):
    def setUp(self):
        cap = _CommandCapture()
        self.ctx = _make_ctx(is_mod=True)
        register_xp_commands(cap.bot, self.ctx)
        self.cap = cap

    def _channel(self, channel_id: int = 400) -> MagicMock:
        ch = MagicMock()
        ch.id = channel_id
        ch.mention = f"<#{channel_id}>"
        return ch

# ---------------------------------------------------------------------------
# Help command tests
# ---------------------------------------------------------------------------


class HelpCommandTests(unittest.TestCase):
    def _run_help(self, *, is_mod: bool, can_grant: bool, can_xp: bool) -> list[str]:
        """Return the list of section names shown in the help dropdown."""
        cap = _CommandCapture()
        ctx = _make_ctx(is_mod=is_mod, can_grant_denizen=can_grant, can_use_xp_grant=can_xp)
        register_mod_commands(cap.bot, ctx)
        ix = _make_interaction()
        _run(cap.get("help")(ix))
        ix.response.send_message.assert_awaited_once()
        view = ix.response.send_message.call_args[1]["view"]
        return [opt.label for opt in view.select.options]

    def test_general_user_sees_limited_help(self):
        sections = self._run_help(is_mod=False, can_grant=False, can_xp=False)
        self.assertIn("General", sections)
        self.assertNotIn("Reports", sections)
        self.assertNotIn("Configuration", sections)

    def test_mod_sees_full_help(self):
        sections = self._run_help(is_mod=True, can_grant=True, can_xp=True)
        self.assertIn("General", sections)
        self.assertIn("Reports", sections)
        self.assertIn("Configuration", sections)

    def test_greeter_sees_greeter_section(self):
        sections = self._run_help(is_mod=False, can_grant=True, can_xp=False)
        self.assertIn("Role Grants", sections)
        self.assertNotIn("Reports", sections)

    def test_help_always_ephemeral(self):
        cap = _CommandCapture()
        ctx = _make_ctx()
        register_mod_commands(cap.bot, ctx)
        ix = _make_interaction()
        _run(cap.get("help")(ix))
        self.assertTrue(ix.response.send_message.call_args[1]["ephemeral"])


if __name__ == "__main__":
    unittest.main()
