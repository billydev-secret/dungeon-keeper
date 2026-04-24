"""Tests for slash command handlers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from commands.denizen_commands import register_denizen_commands
from commands.interaction_commands import register_interaction_commands
from commands.mod_commands import register_mod_commands
from commands.xp_commands import register_xp_commands


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
    ctx.can_grant_denizen = MagicMock(return_value=kwargs.get("can_grant_denizen", False))
    ctx.can_use_xp_grant = MagicMock(return_value=kwargs.get("can_use_xp_grant", False))
    actor = MagicMock()
    actor.id = kwargs.get("actor_id", 100)
    ctx.get_interaction_member = MagicMock(return_value=actor)
    ctx.grant_roles = kwargs.get("grant_roles", {
        "denizen": {"label": "Denizen", "role_id": kwargs.get("denizen_role_id", 0), "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
        "nsfw": {"label": "NSFW", "role_id": 0, "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
        "veteran": {"label": "Veteran", "role_id": 0, "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
        "kink": {"label": "Kink", "role_id": 0, "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
        "goldengirl": {"label": "Golden Girl", "role_id": 0, "log_channel_id": 0, "announce_channel_id": 0, "grant_message": ""},
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


class _AsyncItems:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._iter = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _ScanThread:
    def __init__(self, thread_id: int, messages=None, *, readable: bool = True):
        self.id = thread_id
        self.name = f"thread-{thread_id}"
        self.mention = f"<#{thread_id}>"
        self.guild: Any = None
        self._messages = list(messages or [])
        self._readable = readable

    def permissions_for(self, _member):
        perms = MagicMock()
        perms.read_message_history = self._readable
        return perms

    def history(self, *, limit=None, after=None, oldest_first=True):
        del limit, after, oldest_first
        return _AsyncItems(self._messages)


class _ScanTextChannel:
    def __init__(self, channel_id: int, messages=None, *, archived_threads=None, active_threads=None, readable: bool = True):
        self.id = channel_id
        self.name = f"channel-{channel_id}"
        self.mention = f"<#{channel_id}>"
        self.guild: Any = None
        self._messages = list(messages or [])
        self._archived_threads = list(archived_threads or [])
        self.threads = list(active_threads or [])
        self._readable = readable

    def permissions_for(self, _member):
        perms = MagicMock()
        perms.read_message_history = self._readable
        return perms

    def history(self, *, limit=None, after=None, oldest_first=True):
        del limit, after, oldest_first
        return _AsyncItems(self._messages)

    def archived_threads(self, limit=None):
        del limit
        return _AsyncItems(self._archived_threads)


def _make_scan_message(message_id: int, guild: Any, *, author_id: int = 200) -> MagicMock:
    author = MagicMock()
    author.bot = False
    author.id = author_id
    msg = MagicMock()
    msg.id = message_id
    msg.author = author
    msg.guild = guild
    msg.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    msg.reference = None
    msg.mentions = []
    msg.reactions = []
    msg.attachments = []
    msg.embeds = []
    msg.type = discord.MessageType.default
    msg.content = f"message {message_id}"
    msg.system_content = ""
    return msg


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
    ctx = _make_ctx(can_grant_denizen=True, denizen_role_id=999)
    register_denizen_commands(cap.bot, ctx)
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


# ── XP command permission guard tests ────────────────────────────────

@pytest.fixture
def xp_cap():
    cap = _CommandCapture()
    ctx = _make_ctx()
    register_xp_commands(cap.bot, ctx)
    return cap, ctx


async def test_xp_give_no_permission_denied(xp_cap):
    cap, ctx = xp_cap
    ctx.can_use_xp_grant.return_value = False
    ix = _make_interaction()
    await cap.get("xp_give")(ix, _make_member())
    assert "permission" in ix.response.send_message.call_args[0][0].lower()


async def test_xp_give_bot_target_denied(xp_cap):
    cap, ctx = xp_cap
    ctx.can_use_xp_grant.return_value = True
    ix = _make_interaction(guild=MagicMock())
    await cap.get("xp_give")(ix, _make_member(bot=True))
    assert "bots cannot" in ix.response.send_message.call_args[0][0].lower()


async def test_xp_give_self_grant_denied(xp_cap):
    cap, ctx = xp_cap
    ctx.can_use_xp_grant.return_value = True
    ix = _make_interaction(user_id=200, guild=MagicMock())
    await cap.get("xp_give")(ix, _make_member(user_id=200))
    assert "yourself" in ix.response.send_message.call_args[0][0].lower()


async def test_xp_give_no_guild_denied(xp_cap):
    cap, ctx = xp_cap
    ctx.can_use_xp_grant.return_value = True
    ix = _make_interaction(guild=None)
    await cap.get("xp_give")(ix, _make_member())
    assert "server" in ix.response.send_message.call_args[0][0].lower()


async def test_xp_excluded_channels_non_mod_denied(xp_cap):
    cap, ctx = xp_cap
    ctx.is_mod.return_value = False
    ix = _make_interaction()
    await cap.get("xp_excluded_channels")(ix)
    assert "permission" in ix.response.send_message.call_args[0][0].lower()


async def test_xp_excluded_channels_empty(xp_cap):
    cap, ctx = xp_cap
    ctx.is_mod.return_value = True
    ctx.xp_excluded_channel_ids = set()
    ix = _make_interaction()
    await cap.get("xp_excluded_channels")(ix)
    assert "all channels" in ix.response.send_message.call_args[0][0].lower()


async def test_xp_leaderboards_no_guild_denied(xp_cap):
    cap, _ = xp_cap
    ix = _make_interaction(guild=None)
    await cap.get("xp_leaderboards")(ix)
    assert "server" in ix.response.send_message.call_args[0][0].lower()


async def test_xp_backfill_non_mod_denied(xp_cap):
    cap, ctx = xp_cap
    ctx.is_mod.return_value = False
    ix = _make_interaction()
    await cap.get("xp_backfill_history")(ix)
    assert "permission" in ix.response.send_message.call_args[0][0].lower()


# ── interaction_scan command tests ────────────────────────────────────

@pytest.fixture
def scan_cap():
    cap = _CommandCapture()
    cap.bot.user = MagicMock(id=999)
    ctx = _make_ctx(is_mod=True)
    register_interaction_commands(cap.bot, ctx)
    return cap


async def test_channel_target_only_scans_selected_channel_and_threads(scan_cap):
    cap = scan_cap
    guild = MagicMock()
    guild.id = 123
    guild.get_member = MagicMock(return_value=MagicMock())

    thread = _ScanThread(11)
    target = _ScanTextChannel(10, active_threads=[thread])
    other = _ScanTextChannel(20)
    guild.text_channels = [target, other]
    target.guild = guild
    other.guild = guild
    thread.guild = guild
    target._messages = [_make_scan_message(1001, guild)]
    thread._messages = [_make_scan_message(1002, guild)]
    other._messages = [_make_scan_message(2001, guild)]

    ix = _make_interaction(guild=guild)
    with (
        patch("commands.interaction_commands.clear_interaction_data") as clear_data,
        patch("commands.interaction_commands.store_message") as store_message,
        patch("commands.interaction_commands.set_reaction_count"),
        patch("commands.interaction_commands.record_interactions"),
    ):
        await cap.get("interaction_scan")(ix, days=0, reset=True, channel=target)

    clear_data.assert_called_once()
    scanned_channel_ids = [call.kwargs["channel_id"] for call in store_message.call_args_list]
    assert scanned_channel_ids == [10, 11]
    assert 20 not in scanned_channel_ids
    ix.followup.send.assert_awaited_once()
    message = ix.followup.send.call_args[0][0]
    assert target.mention in message
    assert "Channels scanned: **2**" in message


async def test_channel_target_requires_read_history(scan_cap):
    cap = scan_cap
    guild = MagicMock()
    guild.id = 123
    guild.get_member = MagicMock(return_value=MagicMock())

    unreadable = _ScanTextChannel(30, readable=False)
    unreadable.guild = guild
    guild.text_channels = [unreadable]
    ix = _make_interaction(guild=guild)

    with patch("commands.interaction_commands.store_message") as store_message:
        await cap.get("interaction_scan")(ix, channel=unreadable)

    store_message.assert_not_called()
    ix.followup.send.assert_awaited_once()
    assert "can't read message history" in ix.followup.send.call_args[0][0].lower()


# ── help command tests ────────────────────────────────────────────────

async def _run_help(*, is_mod: bool, can_grant: bool, can_xp: bool) -> list[str]:
    cap = _CommandCapture()
    ctx = _make_ctx(is_mod=is_mod, can_grant_denizen=can_grant, can_use_xp_grant=can_xp)
    register_mod_commands(cap.bot, ctx)
    ix = _make_interaction()
    await cap.get("help")(ix)
    ix.response.send_message.assert_awaited_once()
    view = ix.response.send_message.call_args[1]["view"]
    return [opt.label for opt in view.select.options]


async def test_general_user_sees_limited_help():
    sections = await _run_help(is_mod=False, can_grant=False, can_xp=False)
    assert "General" in sections
    assert "Reports" not in sections
    assert "Configuration" not in sections


async def test_mod_sees_full_help():
    sections = await _run_help(is_mod=True, can_grant=True, can_xp=True)
    assert "General" in sections
    assert "Reports" in sections
    assert "Configuration" in sections


async def test_greeter_sees_greeter_section():
    sections = await _run_help(is_mod=False, can_grant=True, can_xp=False)
    assert "Role Grants" in sections
    assert "Reports" not in sections


async def test_help_always_ephemeral():
    cap = _CommandCapture()
    ctx = _make_ctx()
    register_mod_commands(cap.bot, ctx)
    ix = _make_interaction()
    await cap.get("help")(ix)
    assert ix.response.send_message.call_args[1]["ephemeral"] is True
