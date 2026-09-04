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
    # AllowedMentions' unset fields default to *permitted*, so naming only
    # `roles` would widen every audit card that pings from "pings nobody" to
    # "pings whoever the text happens to name".
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].users is False


# ── The finalizer acts on the policy's own channel, never the press's ─────
#
# ``finalize_policy_vote`` archives and then DELETES the channel it is handed.
# The sweeper resolved it from ``policy["channel_id"]``; the button handler
# passed ``interaction.channel``. Those were the same channel for as long as a
# vote button could only exist inside the proposal channel — the moment one
# lives anywhere else (a community ballot thread, a message a mod copied), the
# button path deletes the wrong channel. Both paths resolve the recorded
# channel now.


def _voting_policy(ctx, *, guild_id: int, channel_id: int) -> int:
    from bot_modules.services.moderation import create_policy_ticket, start_policy_vote

    with open_db(ctx.db_path) as conn:
        pid = create_policy_ticket(
            conn, guild_id=guild_id, creator_id=1, channel_id=channel_id,
            title="t", description="d",
        )
        start_policy_vote(conn, pid, vote_text="v")
    return pid


def _vote_interaction(ctx, *, guild_id: int, policy_channel_id: int, press_channel_id: int):
    """A press arriving from a channel that is NOT the policy's own channel."""
    voter = MagicMock(spec=discord.Member)
    voter.id = 42
    voter.bot = False
    voter.roles = []
    voter.guild_permissions = MagicMock(manage_guild=True, administrator=True)
    # Real strings: the tally embed resolves ids to names and escapes markdown,
    # and re.sub cannot take a MagicMock. Nothing here asserts on the name — it
    # just has to be a name.
    voter.display_name = "Voter"
    voter.name = "voter"

    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.members = [voter]
    # A bare MagicMock answers get_member() for EVERY id, so the tally embed's
    # name resolver would "find" a mock member for every voter and then try to
    # markdown-escape a MagicMock. Behave like a real cache: the voter is
    # present, nobody else is.
    guild.get_member.side_effect = lambda uid: voter if uid == voter.id else None
    voter.guild = guild

    policy_channel = MagicMock(spec=discord.TextChannel)
    policy_channel.id = policy_channel_id
    guild.get_channel.return_value = policy_channel

    press_channel = MagicMock(spec=discord.TextChannel)
    press_channel.id = press_channel_id

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = MagicMock()
    interaction.client.ctx = ctx
    interaction.user = voter
    interaction.guild = guild
    interaction.channel = press_channel
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction, policy_channel, press_channel


@pytest.mark.asyncio
async def test_vote_button_finalizes_against_the_policys_own_channel(
    tmp_path, monkeypatch
):
    ctx = _make_ctx(tmp_path / "jc18.db", guild_id=10)
    policy_id = _voting_policy(ctx, guild_id=10, channel_id=777)
    interaction, policy_channel, press_channel = _vote_interaction(
        ctx, guild_id=10, policy_channel_id=777, press_channel_id=999
    )

    handed: list[object] = []

    async def _fake_finalize(ctx_, guild, pid, outcome, *, channel, **kw):
        handed.append(channel)
        return True

    monkeypatch.setattr(jc, "finalize_policy_vote", _fake_finalize)

    await jc._handle_policy_vote(interaction, policy_id, "yes")

    assert handed, "the sole eligible voter voting yes must finalize the vote"
    assert handed[0] is policy_channel
    assert handed[0] is not press_channel


@pytest.mark.asyncio
async def test_vote_button_hands_no_channel_when_the_policys_own_is_gone(
    tmp_path, monkeypatch
):
    """A deleted proposal channel must finalize with ``channel=None`` — never
    fall back to whichever channel the press came from."""
    ctx = _make_ctx(tmp_path / "jc19.db", guild_id=10)
    policy_id = _voting_policy(ctx, guild_id=10, channel_id=777)
    interaction, _policy_channel, _press = _vote_interaction(
        ctx, guild_id=10, policy_channel_id=777, press_channel_id=999
    )
    interaction.guild.get_channel.return_value = None

    handed: list[object] = []

    async def _fake_finalize(ctx_, guild, pid, outcome, *, channel, **kw):
        handed.append(channel)
        return True

    monkeypatch.setattr(jc, "finalize_policy_vote", _fake_finalize)

    await jc._handle_policy_vote(interaction, policy_id, "yes")

    assert handed == [None]


# ── One roster rule, one implementation ───────────────────────────────────


def test_guild_eligible_voters_matches_the_shared_helper(tmp_path):
    """The three sites that used to rebuild the roster inline now share
    ``jail.logic.eligible_voters`` — bots out, administrators in, configured
    mod/admin role holders in, everyone else out."""
    from bot_modules.jail.logic import eligible_voters

    ctx = _make_ctx(tmp_path / "jc20.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "100", guild_id=10)
        _db_set(conn, "admin_role_ids", "200", guild_id=10)

    def _m(uid, *, roles=(), admin=False, bot=False):
        m = MagicMock(spec=discord.Member)
        m.id = uid
        m.bot = bot
        m.roles = [MagicMock(id=r) for r in roles]
        m.guild_permissions = MagicMock(administrator=admin)
        return m

    members = [
        _m(1, admin=True),             # administrator
        _m(2, roles=[100]),            # mod role
        _m(3, roles=[200]),            # admin role
        _m(4, roles=[999]),            # unrelated role
        _m(5, roles=[100], bot=True),  # a bot holding a mod role
    ]
    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.members = members

    assert jc.guild_eligible_voters(ctx, guild) == {1, 2, 3}
    assert jc.guild_eligible_voters(ctx, guild) == eligible_voters(
        [
            {
                "user_id": m.id,
                "is_bot": m.bot,
                "role_ids": [r.id for r in m.roles],
                "is_administrator": m.guild_permissions.administrator,
            }
            for m in members
        ],
        {100},
        {200},
    )


# ── Community ballots ─────────────────────────────────────────────────


def _open_ballot_row(ctx, *, closes_at: float = 0.0, guild_id: int = 10) -> int:
    from bot_modules.services import policy_ballot_service as pbs
    from bot_modules.services.moderation import create_policy_ticket

    with open_db(ctx.db_path) as conn:
        policy_id = create_policy_ticket(
            conn, guild_id=guild_id, creator_id=1, channel_id=0,
            title="q", description="",
        )
        return pbs.open_ballot(
            conn, guild_id=guild_id, policy_id=policy_id, channel_id=500,
            question="Quiet hours?", opened_by=1, closes_at=closes_at,
        )


def _presser(ctx, *, can_view: bool = True, is_bot: bool = False, admin: bool = False):
    member = MagicMock(spec=discord.Member)
    member.id = 42
    member.bot = is_bot
    member.roles = []
    member.guild_permissions = MagicMock(manage_guild=admin, administrator=admin)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.members = [member]
    # No member cache: the name resolver falls through to `known_users` and
    # then to a mention, which is what the accent/name paths under test want.
    guild.get_member = MagicMock(return_value=None)
    guild.icon = None
    member.guild = guild

    channel = MagicMock(spec=discord.Thread)
    channel.id = 600
    channel.permissions_for = MagicMock(
        return_value=MagicMock(view_channel=can_view)
    )

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = MagicMock()
    interaction.client.ctx = ctx
    interaction.user = member
    interaction.guild = guild
    interaction.channel = channel
    interaction.channel_id = channel.id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_a_ballot_press_records_the_vote(tmp_path):
    from bot_modules.services import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b1.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    interaction = _presser(ctx)

    await jc._handle_ballot_vote(interaction, ballot_id, "yes")

    with open_db(ctx.db_path) as conn:
        assert pbs.tally_ballot(conn, ballot_id)["yes"] == [42]
    interaction.response.edit_message.assert_awaited()


@pytest.mark.asyncio
async def test_a_presser_who_cannot_see_the_thread_is_refused(tmp_path):
    """Visibility *is* the electorate, so it is re-read from Discord on every
    press: a member who lost the role that let them into the channel stops
    being able to vote from that moment."""
    from bot_modules.services import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b2.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    interaction = _presser(ctx, can_view=False)

    await jc._handle_ballot_vote(interaction, ballot_id, "yes")

    with open_db(ctx.db_path) as conn:
        assert pbs.get_ballot_votes(conn, ballot_id) == []
    assert "❌" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_a_press_on_a_closed_ballot_is_refused(tmp_path):
    from bot_modules.services import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b3.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.close_ballot(conn, ballot_id, closed_by=1)
    interaction = _presser(ctx)

    await jc._handle_ballot_vote(interaction, ballot_id, "yes")

    with open_db(ctx.db_path) as conn:
        assert pbs.get_ballot_votes(conn, ballot_id) == []


@pytest.mark.asyncio
async def test_the_close_button_refuses_a_non_moderator(tmp_path):
    """The Close button sits on a card everyone in the thread can see, so it
    cannot rely on being invisible to members — it refuses on press."""
    from bot_modules.services import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b4.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    interaction = _presser(ctx, admin=False)

    await jc.PolicyBallotCloseButton(ballot_id).callback(interaction)

    with open_db(ctx.db_path) as conn:
        ballot = pbs.get_ballot(conn, ballot_id)
        assert ballot is not None and pbs.is_open(ballot)
    assert "❌" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_the_close_button_freezes_the_result_for_a_moderator(tmp_path):
    from bot_modules.services import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b5.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=10, user_id=1, choice="yes"
        )
    interaction = _presser(ctx, admin=True)

    await jc.PolicyBallotCloseButton(ballot_id).callback(interaction)

    with open_db(ctx.db_path) as conn:
        ballot = pbs.get_ballot(conn, ballot_id)
        assert ballot is not None
        assert ballot["outcome"] == pbs.OUTCOME_PASSED
        assert ballot["closed_by"] == 42
        # A ballot is recorded as a policy ticket, so closing it must close
        # that ticket too — otherwise a decided ballot sits in the dashboard's
        # live queue forever.
        ticket = conn.execute(
            "SELECT status FROM policy_tickets WHERE id = ?", (ballot["policy_id"],)
        ).fetchone()
        assert ticket["status"] == "closed"


@pytest.mark.asyncio
async def test_a_ballot_never_writes_a_policy_row(tmp_path):
    """A passed ballot is *recorded*, not enacted — Billy's decision. Nothing
    reaches `policies`, the table `/policy list` reads."""
    from bot_modules.services import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b6.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=10, user_id=1, choice="yes"
        )
    interaction = _presser(ctx, admin=True)

    await jc.PolicyBallotCloseButton(ballot_id).callback(interaction)

    with open_db(ctx.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_the_ballot_card_is_rendered_with_resolved_names(tmp_path):
    """The render-site guard: a ``<@id>`` in an embed read by ordinary members
    degrades to a bare number for anyone who has not seen that member."""
    from bot_modules.services import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b7.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=10, user_id=7, choice="yes"
        )
        ballot = pbs.get_ballot(conn, ballot_id)
        tally = pbs.tally_ballot(conn, ballot_id)
    assert ballot is not None

    named = MagicMock(spec=discord.Member)
    named.display_name = "Ada"
    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.get_member = MagicMock(return_value=named)

    embed = await jc._render_ballot_embed(ctx, guild, ballot, tally)

    yes_field = next(f for f in embed.fields if f.name.startswith("✅"))
    assert "Ada" in (yes_field.value or "")
    assert "<@7>" not in (yes_field.value or "")


@pytest.mark.asyncio
async def test_the_sweep_closes_ballots_past_their_deadline(tmp_path, monkeypatch):
    """Ballots ride the existing 60-second policy sweep rather than getting a
    second loop over every guild."""
    ctx = _make_ctx(tmp_path / "b8.db", guild_id=10)
    due = _open_ballot_row(ctx, closes_at=100.0)
    not_due = _open_ballot_row(ctx, closes_at=9_999_999_999.0)
    no_deadline = _open_ballot_row(ctx, closes_at=0.0)

    closed: list[int] = []

    async def _fake_finalize(ctx_, guild, ballot_id, *, closed_by, cancelled=False):
        closed.append(ballot_id)
        return None

    monkeypatch.setattr(jc, "finalize_ballot", _fake_finalize)
    monkeypatch.setattr(jc, "_policy_vote_timeout_pass", AsyncMock())

    bot = MagicMock()
    bot.guilds = [MagicMock(id=10)]
    await jc.sweep_expired_policy_votes(bot, ctx)

    assert closed == [due]
    assert not_due not in closed
    assert no_deadline not in closed


@pytest.mark.asyncio
async def test_policy_ballot_command_refuses_a_non_admin_at_runtime(tmp_path):
    """`/policy`'s ``default_permissions`` decorators are inert — discord.py
    only emits ``default_member_permissions`` for top-level commands and the
    group carries none — so every member already sees ``/policy ballot`` in
    the picker. The runtime check is the only gate there is."""
    from bot_modules.cogs.jail_cog import JailCog

    ctx = _make_ctx(tmp_path / "b9.db", guild_id=10)
    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=False)
    interaction.channel = MagicMock(spec=discord.TextChannel)

    await cog.policy_ballot_cmd.callback(cog, interaction)

    interaction.response.send_modal.assert_not_awaited()
    assert "❌" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_policy_ballot_command_refuses_outside_a_text_channel(tmp_path):
    """A ballot opens a thread, and a thread cannot hold one."""
    from bot_modules.cogs.jail_cog import JailCog

    ctx = _make_ctx(tmp_path / "b10.db", guild_id=10)
    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=True)  # channel is a Thread

    await cog.policy_ballot_cmd.callback(cog, interaction)

    interaction.response.send_modal.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_ballot_command_opens_the_modal_for_an_admin(tmp_path):
    from bot_modules.cogs.jail_cog import JailCog

    ctx = _make_ctx(tmp_path / "b11.db", guild_id=10)
    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=True)
    interaction.channel = MagicMock(spec=discord.TextChannel)

    await cog.policy_ballot_cmd.callback(cog, interaction)

    interaction.response.send_modal.assert_awaited()


@pytest.mark.asyncio
async def test_policy_close_in_a_ballot_thread_cancels_the_ballot(tmp_path):
    """A ballot's own ticket wears status 'ballot', so the ordinary proposal
    lookup can never reach it — `/policy close` finds it by thread instead and
    records the votes cast without claiming a result."""
    from bot_modules.cogs.jail_cog import JailCog
    from bot_modules.services import policy_ballot_service as pbs

    ctx = _make_ctx(tmp_path / "b12.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        pbs.attach_ballot_message(conn, ballot_id, thread_id=600, message_id=601)
        pbs.cast_ballot_vote(
            conn, ballot_id=ballot_id, guild_id=10, user_id=1, choice="yes"
        )

    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=True)
    interaction.guild.get_channel_or_thread = MagicMock(return_value=None)

    await cog.policy_close_cmd.callback(cog, interaction, None)

    with open_db(ctx.db_path) as conn:
        ballot = pbs.get_ballot(conn, ballot_id)
        assert ballot is not None
        assert ballot["outcome"] == pbs.OUTCOME_CANCELLED
        # The counts are still frozen: the record says what the room had said.
        assert pbs.frozen_counts(ballot) == (1, 0, 0)


@pytest.mark.asyncio
async def test_policy_vote_cannot_be_started_inside_a_ballot_thread(tmp_path):
    """The mod team's unanimity vote archives and deletes the channel it runs
    in when it resolves. A ballot thread must never be reachable by it."""
    from bot_modules.cogs.jail_cog import JailCog
    from bot_modules.services import policy_ballot_service as pbs
    from bot_modules.services.moderation import get_policy_ticket_by_channel

    ctx = _make_ctx(tmp_path / "b13.db", guild_id=10)
    ballot_id = _open_ballot_row(ctx)
    with open_db(ctx.db_path) as conn:
        ballot = pbs.get_ballot(conn, ballot_id)
        assert ballot is not None
        conn.execute(
            "UPDATE policy_tickets SET channel_id = ?, status = ? WHERE id = ?",
            (600, pbs.TICKET_STATUS_BALLOT, ballot["policy_id"]),
        )
        assert get_policy_ticket_by_channel(conn, 600) is None

    bot = MagicMock()
    bot.ctx = ctx
    cog = JailCog(bot)
    interaction = _presser(ctx, admin=True)

    await cog.policy_vote_cmd.callback(cog, interaction)

    interaction.response.send_modal.assert_not_awaited()
