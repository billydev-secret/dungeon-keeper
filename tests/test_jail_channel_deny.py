"""Tests for the Jailed-role channel-visibility backstop.

Jail is a deny-list: a jailed member keeps @everyone, so any channel without an
explicit ``@Jailed → view_channel=False`` overwrite leaks to them. The initial
stamp only runs when the Jailed role is created, so channels made later (like a
new category) are exposed. These tests cover the two closers:

* ``stamp_channel_jail_deny`` — deny one channel unless already denied.
* ``jail_channel_deny_sweep`` — startup backfill over every guild channel.
* ``JailCog._deny_jailed_on_new_channel`` — the on_guild_channel_create glue,
  including its guard branches (wrong guild, no role configured, role gone).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord

from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import open_db, set_config_value as _db_set
from bot_modules.commands.jail_commands import (
    jail_channel_deny_sweep,
    stamp_channel_jail_deny,
)
from bot_modules.cogs.jail_cog import JailCog
from migrations import apply_migrations_sync


# ── Fixtures ────────────────────────────────────────────────────────


def _make_ctx(db_path, *, guild_id: int = 100) -> AppContext:
    apply_migrations_sync(db_path)
    return AppContext(
        bot=MagicMock(),
        log=logging.getLogger("test"),
        db_path=db_path,
        guild_id=guild_id,
        debug=True,
    )


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(MagicMock(status=403, reason="Forbidden"), {"code": 50013})


def _channel(cid: int, view: bool | None, *, forbidden: bool = False) -> MagicMock:
    """A GuildChannel-shaped mock whose Jailed overwrite reports ``view``."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = cid
    ch.name = f"c{cid}"
    overwrite = MagicMock()
    overwrite.view_channel = view
    ch.overwrites_for = MagicMock(return_value=overwrite)
    ch.set_permissions = AsyncMock(
        side_effect=_forbidden() if forbidden else None
    )
    return ch


def _role(rid: int = 5000) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = rid
    return role


# ── stamp_channel_jail_deny ──────────────────────────────────────────


async def test_stamp_denies_channel_with_no_overwrite():
    role = _role()
    ch = _channel(1, None)
    assert await stamp_channel_jail_deny(ch, role) is True
    ch.set_permissions.assert_awaited_once()
    kwargs = ch.set_permissions.call_args.kwargs
    assert kwargs["view_channel"] is False
    assert kwargs["send_messages"] is False


async def test_stamp_overrides_explicitly_allowed_channel():
    role = _role()
    ch = _channel(1, True)  # someone allowed @Jailed to view — must be flipped
    assert await stamp_channel_jail_deny(ch, role) is True
    ch.set_permissions.assert_awaited_once()


async def test_stamp_skips_already_denied_channel():
    role = _role()
    ch = _channel(1, False)
    assert await stamp_channel_jail_deny(ch, role) is False
    ch.set_permissions.assert_not_awaited()


async def test_stamp_swallows_forbidden():
    role = _role()
    ch = _channel(1, None, forbidden=True)
    # Missing Manage Roles on this channel must not raise — best-effort.
    assert await stamp_channel_jail_deny(ch, role) is False


# ── jail_channel_deny_sweep ──────────────────────────────────────────


def _bot_with_guild(guild) -> MagicMock:
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.get_guild = MagicMock(return_value=guild)
    return bot


async def test_sweep_stamps_only_exposed_channels(tmp_path):
    ctx = _make_ctx(tmp_path / "sweep.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    role = _role()
    exposed = _channel(10, None)
    allowed = _channel(20, True)
    denied = _channel(30, False)

    guild = MagicMock(spec=discord.Guild)
    guild.id = ctx.guild_id
    guild.get_role = MagicMock(return_value=role)
    guild.channels = [exposed, allowed, denied]
    by_id = {c.id: c for c in guild.channels}
    guild.get_channel = MagicMock(side_effect=lambda cid: by_id.get(cid))

    await jail_channel_deny_sweep(_bot_with_guild(guild), ctx)

    exposed.set_permissions.assert_awaited_once()
    allowed.set_permissions.assert_awaited_once()
    denied.set_permissions.assert_not_awaited()  # already denied → untouched


async def test_sweep_noop_when_no_jailed_role_configured(tmp_path):
    ctx = _make_ctx(tmp_path / "sweep_norole.db")
    # jailed_role_id defaults to 0 → nothing to do.
    ch = _channel(10, None)
    guild = MagicMock(spec=discord.Guild)
    guild.id = ctx.guild_id
    guild.get_role = MagicMock(return_value=None)
    guild.channels = [ch]

    await jail_channel_deny_sweep(_bot_with_guild(guild), ctx)
    ch.set_permissions.assert_not_awaited()


async def test_sweep_noop_when_guild_missing(tmp_path):
    ctx = _make_ctx(tmp_path / "sweep_noguild.db")
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.get_guild = MagicMock(return_value=None)
    # Must not raise despite the bot not being in the guild.
    await jail_channel_deny_sweep(bot, ctx)


# ── on_guild_channel_create listener ─────────────────────────────────


def _cog(ctx) -> JailCog:
    return JailCog(MagicMock(), ctx)


async def test_listener_stamps_new_channel(tmp_path):
    ctx = _make_ctx(tmp_path / "lis.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    role = _role()
    guild = MagicMock(spec=discord.Guild)
    guild.id = ctx.guild_id
    guild.get_role = MagicMock(return_value=role)

    ch = _channel(10, None)
    ch.guild = guild

    await _cog(ctx)._deny_jailed_on_new_channel(ch)
    ch.set_permissions.assert_awaited_once()


async def test_listener_ignores_other_guild(tmp_path):
    ctx = _make_ctx(tmp_path / "lis_other.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    guild = MagicMock(spec=discord.Guild)
    guild.id = ctx.guild_id + 999  # different guild
    ch = _channel(10, None)
    ch.guild = guild

    await _cog(ctx)._deny_jailed_on_new_channel(ch)
    ch.set_permissions.assert_not_awaited()


async def test_listener_noop_without_configured_role(tmp_path):
    ctx = _make_ctx(tmp_path / "lis_norole.db")
    # No jailed_role_id set → guard returns before touching the channel.
    guild = MagicMock(spec=discord.Guild)
    guild.id = ctx.guild_id
    guild.get_role = MagicMock(return_value=None)
    ch = _channel(10, None)
    ch.guild = guild

    await _cog(ctx)._deny_jailed_on_new_channel(ch)
    ch.set_permissions.assert_not_awaited()
