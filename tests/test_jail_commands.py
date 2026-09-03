"""Tests for bot_modules.commands.jail_commands helper functions.

Focused on the prelaunch P0 fix: ``_get_mod_role_ids``/``_get_admin_role_ids``
take an explicit ``guild_id``, and ``_is_mod``/``_is_admin`` look up roles
via ``ctx.guild_config(member.guild.id)`` rather than the home-guild flat
fields on ``AppContext``. Without this scoping, a 2nd guild would silently
inherit the home guild's mod/admin role list.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot_modules.commands.jail_commands as jc

from bot_modules.commands.jail_commands import (
    _get_admin_role_ids,
    _get_config,
    _get_mod_role_ids,
    _is_admin,
    _is_mod,
)
from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import open_db, set_config_value as _db_set
from tests.db_template import migrated_db


def _make_ctx(db_path, guild_id: int = 10) -> AppContext:
    migrated_db(db_path)
    return AppContext(
        bot=MagicMock(),
        log=logging.getLogger("test"),
        db_path=db_path,
        guild_id=guild_id,
        debug=True,
    )


def _member(role_ids: list[int], *, manage_guild=False, administrator=False, guild_id=10):
    m = MagicMock()
    m.roles = [MagicMock(id=rid) for rid in role_ids]
    m.guild_permissions = MagicMock(manage_guild=manage_guild, administrator=administrator)
    m.guild = MagicMock()
    m.guild.id = guild_id
    return m


# ── _get_mod_role_ids / _get_admin_role_ids ──────────────────────────


def test_get_mod_role_ids_scoped_to_guild(tmp_path):
    """_get_mod_role_ids must use the guild_id arg, not the home guild flats."""
    ctx = _make_ctx(tmp_path / "jc1.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "100,101", guild_id=10)
        _db_set(conn, "mod_role_ids", "200,201", guild_id=20)

    assert _get_mod_role_ids(ctx, 10) == {100, 101}
    assert _get_mod_role_ids(ctx, 20) == {200, 201}


def test_get_admin_role_ids_scoped_to_guild(tmp_path):
    ctx = _make_ctx(tmp_path / "jc2.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "admin_role_ids", "500", guild_id=10)
        _db_set(conn, "admin_role_ids", "600,601", guild_id=20)

    assert _get_admin_role_ids(ctx, 10) == {500}
    assert _get_admin_role_ids(ctx, 20) == {600, 601}


def test_get_mod_role_ids_returns_empty_for_unconfigured_non_home_guild(tmp_path):
    """Unconfigured 2nd guild must NOT inherit home-guild roles via legacy fallback."""
    ctx = _make_ctx(tmp_path / "jc3.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "100", guild_id=0)  # legacy
        _db_set(conn, "mod_role_ids", "111", guild_id=10)  # home

    assert _get_mod_role_ids(ctx, 20) == set()


# ── _is_mod / _is_admin ──────────────────────────────────────────────


def test_is_mod_true_when_member_has_configured_mod_role(tmp_path):
    ctx = _make_ctx(tmp_path / "jc4.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "777", guild_id=10)

    member = _member(role_ids=[777], guild_id=10)
    assert _is_mod(member, ctx) is True


def test_is_mod_true_for_manage_guild_short_circuit(tmp_path):
    ctx = _make_ctx(tmp_path / "jc5.db", guild_id=10)
    member = _member(role_ids=[], manage_guild=True, guild_id=10)
    assert _is_mod(member, ctx) is True


def test_is_mod_true_for_administrator_short_circuit(tmp_path):
    ctx = _make_ctx(tmp_path / "jc6.db", guild_id=10)
    member = _member(role_ids=[], administrator=True, guild_id=10)
    assert _is_mod(member, ctx) is True


def test_is_mod_false_for_unrelated_role(tmp_path):
    ctx = _make_ctx(tmp_path / "jc7.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "777", guild_id=10)

    member = _member(role_ids=[123], guild_id=10)
    assert _is_mod(member, ctx) is False


def test_is_mod_reads_role_from_members_own_guild(tmp_path):
    """A member in guild 20 must be evaluated against guild 20's roles, not home."""
    ctx = _make_ctx(tmp_path / "jc8.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "777", guild_id=10)  # home only
        _db_set(conn, "mod_role_ids", "888", guild_id=20)  # other guild

    member_in_20_with_888 = _member(role_ids=[888], guild_id=20)
    assert _is_mod(member_in_20_with_888, ctx) is True

    member_in_20_with_777 = _member(role_ids=[777], guild_id=20)
    assert _is_mod(member_in_20_with_777, ctx) is False  # home roles don't leak


def test_is_admin_true_for_administrator_short_circuit(tmp_path):
    ctx = _make_ctx(tmp_path / "jc9.db", guild_id=10)
    member = _member(role_ids=[], administrator=True, guild_id=10)
    assert _is_admin(member, ctx) is True


def test_is_admin_false_for_mod_role_only(tmp_path):
    """Mod role does NOT grant admin in _is_admin (the inverse direction does grant
    mod via member_is_mod, but member_is_admin requires admin_role_ids)."""
    ctx = _make_ctx(tmp_path / "jc10.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "777", guild_id=10)

    member = _member(role_ids=[777], guild_id=10)
    assert _is_admin(member, ctx) is False


def test_is_admin_true_for_configured_admin_role(tmp_path):
    ctx = _make_ctx(tmp_path / "jc11.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "admin_role_ids", "999", guild_id=10)

    member = _member(role_ids=[999], guild_id=10)
    assert _is_admin(member, ctx) is True


# ── _get_config guild scoping ────────────────────────────────────────


def test_get_config_uses_guild_id_arg(tmp_path):
    ctx = _make_ctx(tmp_path / "jc12.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "warning_threshold", "5", guild_id=10)
        _db_set(conn, "warning_threshold", "7", guild_id=20)

    assert _get_config(ctx, "warning_threshold", "3", guild_id=10) == 5
    assert _get_config(ctx, "warning_threshold", "3", guild_id=20) == 7


def test_get_config_returns_default_when_unset(tmp_path):
    ctx = _make_ctx(tmp_path / "jc13.db", guild_id=10)
    assert _get_config(ctx, "warning_threshold", "3", guild_id=10) == 3


# ── warning threshold read honors the guild-scoped row ───────────────
#
# The dashboard writes ``warning_threshold`` per-guild; the jail cog's
# threshold-alert read must pass ``guild_id`` or it silently reads the
# legacy ``guild_id=0`` row and the hard-coded "3" default always wins.


def test_read_warning_threshold_uses_guild_scoped_row(tmp_path):
    from bot_modules.cogs.jail_cog import _read_warning_threshold

    ctx = _make_ctx(tmp_path / "jc14.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "warning_threshold", "3", guild_id=0)  # legacy
        _db_set(conn, "warning_threshold", "5", guild_id=10)

    # The configured guild row must win over the legacy default of 3.
    assert _read_warning_threshold(ctx, 10) == 5


def test_read_warning_threshold_defaults_to_three(tmp_path):
    from bot_modules.cogs.jail_cog import _read_warning_threshold

    ctx = _make_ctx(tmp_path / "jc15.db", guild_id=10)
    assert _read_warning_threshold(ctx, 10) == 3


# ── policy vote timeout sweeps every guild ───────────────────────────
#
# ``/policy open`` works on any server and each server sets its own voting
# deadline on its own dashboard, so a sweep that only ever looked at the home
# guild left proposals elsewhere stuck in 'voting' forever.


async def test_policy_vote_sweep_resolves_on_every_guild(tmp_path, monkeypatch):
    import time

    from bot_modules.commands import jail_commands as jc
    from bot_modules.services.moderation import (
        create_policy_ticket,
        start_policy_vote,
    )

    ctx = _make_ctx(tmp_path / "jc16.db", guild_id=10)
    pids: dict[int, int] = {}
    with open_db(ctx.db_path) as conn:
        for gid in (10, 20):
            pid = create_policy_ticket(
                conn, guild_id=gid, creator_id=1, channel_id=5,
                title="t", description="d",
            )
            start_policy_vote(conn, pid, vote_text="v")
            conn.execute(
                "UPDATE policy_tickets SET vote_started_at = ? WHERE id = ?",
                (time.time() - 10 * 24 * 3600, pid),
            )
            pids[gid] = pid

    resolved: list[tuple[int, int]] = []

    async def _fake_resolve(bot, ctx_, guild, policy):
        resolved.append((guild.id, policy["id"]))

    monkeypatch.setattr(jc, "_resolve_expired_policy", _fake_resolve)

    bot = MagicMock()
    bot.guilds = [MagicMock(id=10), MagicMock(id=20)]

    await jc.sweep_expired_policy_votes(bot, ctx)

    assert sorted(resolved) == [(10, pids[10]), (20, pids[20])]


async def test_policy_vote_sweep_honors_a_per_guild_deadline_of_zero(
    tmp_path, monkeypatch
):
    """A guild that sets the deadline to 0 turns auto-resolution off — for
    itself only."""
    import time

    from bot_modules.commands import jail_commands as jc
    from bot_modules.services.moderation import (
        create_policy_ticket,
        start_policy_vote,
    )

    ctx = _make_ctx(tmp_path / "jc17.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "policy_vote_timeout_hours", "0", guild_id=20)
        for gid in (10, 20):
            pid = create_policy_ticket(
                conn, guild_id=gid, creator_id=1, channel_id=5,
                title="t", description="d",
            )
            start_policy_vote(conn, pid, vote_text="v")
            conn.execute(
                "UPDATE policy_tickets SET vote_started_at = ? WHERE id = ?",
                (time.time() - 10 * 24 * 3600, pid),
            )

    resolved: list[int] = []

    async def _fake_resolve(bot, ctx_, guild, policy):
        resolved.append(guild.id)

    monkeypatch.setattr(jc, "_resolve_expired_policy", _fake_resolve)

    bot = MagicMock()
    bot.guilds = [MagicMock(id=10), MagicMock(id=20)]

    await jc.sweep_expired_policy_votes(bot, ctx)

    assert resolved == [10]


# ── _post_audit: record cards, and pings that actually reach someone ──────


def _audit_channel_ctx(tmp_path):
    """A ctx with a mod-log channel, plus the guild/channel mocks it resolves."""
    ctx = _make_ctx(str(tmp_path / "audit.db"))
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "log_channel_id", "555", guild_id=10)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.id = 10
    guild.get_channel.return_value = channel
    return ctx, guild, channel


@pytest.mark.asyncio
async def test_post_audit_stamps_a_record_card(tmp_path):
    """Audit cards are the mod-log's scrollback, so they carry a timestamp.

    Stamped here rather than in each of the ~20 builders — which is how 17 of
    them came to be missing one. → embed_style_guide.md § Timestamps
    """
    ctx, guild, channel = _audit_channel_ctx(tmp_path)
    embed = discord.Embed(title="🔒 Member Jailed")
    assert embed.timestamp is None

    await jc._post_audit(ctx, guild, embed)

    sent = channel.send.await_args.kwargs["embed"]
    assert sent.timestamp is not None


@pytest.mark.asyncio
async def test_post_audit_keeps_a_timestamp_the_builder_already_set(tmp_path):
    ctx, guild, channel = _audit_channel_ctx(tmp_path)
    stamped = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    embed = discord.Embed(title="📩 Ticket Opened", timestamp=stamped)

    await jc._post_audit(ctx, guild, embed)

    assert channel.send.await_args.kwargs["embed"].timestamp == stamped


@pytest.mark.asyncio
async def test_post_audit_defaults_to_pinging_nobody(tmp_path):
    ctx, guild, channel = _audit_channel_ctx(tmp_path)

    await jc._post_audit(ctx, guild, discord.Embed(title="🔓 Member Released"))

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] is None
    assert kwargs["allowed_mentions"].roles is False


@pytest.mark.asyncio
async def test_post_audit_allow_lists_exactly_the_roles_it_pings(tmp_path):
    """The warning-threshold alert used to put its pings in the embed, where a
    mention notifies nobody. They ride in ``content=`` now, and the send has to
    allow-list them or Discord suppresses them anyway."""
    ctx, guild, channel = _audit_channel_ctx(tmp_path)

    await jc._post_audit(
        ctx,
        guild,
        discord.Embed(title="🚨 Warning Threshold Reached"),
        content="<@&100> <@&200>",
        ping_role_ids=[100, 200],
    )

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "<@&100> <@&200>"
    assert [r.id for r in kwargs["allowed_mentions"].roles] == [100, 200]
