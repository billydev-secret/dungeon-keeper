"""Tests for bot_modules.jail.apply — the canonical jail-application flow.

Covers ``check_jail_preconditions`` (pure validation) and ``apply_jail`` (the
full Discord-orchestration coroutine). Discord objects are mocked since the
function's whole purpose is to abstract those interactions.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord

from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import open_db, set_config_value as _db_set
from bot_modules.jail.apply import apply_jail, check_jail_preconditions
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
        mod_channel_id=0,
        spoiler_required_channels=set(),
        bypass_role_ids=set(),
        xp_grant_allowed_user_ids=set(),
        xp_excluded_channel_ids=set(),
        recorded_bot_user_ids=set(),
        level_5_role_id=0,
        level_5_log_channel_id=0,
        level_up_log_channel_id=0,
        greeter_role_id=0,
        greeter_chat_channel_id=0,
        join_leave_log_channel_id=0,
        welcome_channel_id=0,
        welcome_message="",
        welcome_ping_role_id=0,
        leave_channel_id=0,
        leave_message="",
    )


def _member(
    member_id: int,
    *,
    is_bot: bool = False,
    admin: bool = False,
    manage_guild: bool = False,
    role_ids: tuple[int, ...] = (),
    name: str | None = None,
) -> MagicMock:
    """Build a discord.Member-shaped mock with the perms and roles given."""
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = is_bot
    m.name = name or f"u{member_id}"
    m.display_name = m.name
    m.mention = f"<@{member_id}>"
    perms = MagicMock()
    perms.administrator = admin
    perms.manage_guild = manage_guild
    m.guild_permissions = perms
    roles = []
    for rid in role_ids:
        role = MagicMock(spec=discord.Role)
        role.id = rid
        roles.append(role)
    m.roles = roles
    m.edit = AsyncMock()
    m.send = AsyncMock()
    return m


def _guild(*, guild_id: int = 100, members: list | None = None) -> MagicMock:
    """Build a discord.Guild-shaped mock with the given members."""
    members = members or []
    by_id = {m.id: m for m in members}
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    g.name = "Test Guild"
    g.get_member = MagicMock(side_effect=lambda uid: by_id.get(int(uid)))
    default_role = MagicMock(spec=discord.Role)
    default_role.id = 0
    g.default_role = default_role
    g.roles = [default_role]
    g.channels = []
    g.get_role = MagicMock(return_value=None)
    g.get_channel = MagicMock(return_value=None)
    g.create_role = AsyncMock()
    g.create_text_channel = AsyncMock()
    g.me = _member(99, name="bot")
    return g


# ── check_jail_preconditions ─────────────────────────────────────────


def test_precheck_rejects_bot_target(tmp_path):
    ctx = _make_ctx(tmp_path / "p1.db")
    guild = _guild()
    target = _member(42, is_bot=True)
    moderator = _member(1)
    result = check_jail_preconditions(ctx, guild, target, moderator)
    assert result is not None
    assert result.error_kind == "bot_target"


def test_precheck_rejects_self_jail(tmp_path):
    ctx = _make_ctx(tmp_path / "p2.db")
    guild = _guild()
    me = _member(7)
    result = check_jail_preconditions(ctx, guild, me, me)
    assert result is not None
    assert result.error_kind == "self_target"


def test_precheck_rejects_admin_target_via_perm(tmp_path):
    ctx = _make_ctx(tmp_path / "p3.db")
    guild = _guild()
    admin_target = _member(42, admin=True)
    moderator = _member(1)
    result = check_jail_preconditions(ctx, guild, admin_target, moderator)
    assert result is not None
    assert result.error_kind == "admin_target"


def test_precheck_rejects_admin_target_via_configured_role(tmp_path):
    """Admin role IDs are loaded via guild_config; target with the role is
    classified as admin even without the Discord ADMINISTRATOR perm."""
    ctx = _make_ctx(tmp_path / "p4.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "admin_role_ids", "555", guild_id=ctx.guild_id)

    guild = _guild()
    target = _member(42, role_ids=(555,))
    moderator = _member(1)
    result = check_jail_preconditions(ctx, guild, target, moderator)
    assert result is not None
    assert result.error_kind == "admin_target"


def test_precheck_rejects_mod_target_when_actor_is_not_admin(tmp_path):
    """A mod can be jailed only by an admin — not by another mod."""
    ctx = _make_ctx(tmp_path / "p5.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "777", guild_id=ctx.guild_id)
        _db_set(conn, "admin_role_ids", "999", guild_id=ctx.guild_id)

    guild = _guild()
    mod_target = _member(42, role_ids=(777,))
    mod_actor = _member(1, role_ids=(777,))  # mod but not admin
    result = check_jail_preconditions(ctx, guild, mod_target, mod_actor)
    assert result is not None
    assert result.error_kind == "mod_target"


def test_precheck_allows_admin_to_jail_a_mod(tmp_path):
    """Admin actor can jail a moderator."""
    ctx = _make_ctx(tmp_path / "p6.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "777", guild_id=ctx.guild_id)
        _db_set(conn, "admin_role_ids", "999", guild_id=ctx.guild_id)

    guild = _guild()
    mod_target = _member(42, role_ids=(777,))
    admin_actor = _member(1, role_ids=(999,))
    result = check_jail_preconditions(ctx, guild, mod_target, admin_actor)
    assert result is None  # OK to proceed


def test_precheck_rejects_already_jailed_target(tmp_path):
    """An existing active jail row blocks a second jail attempt."""
    from bot_modules.services.moderation import create_jail

    ctx = _make_ctx(tmp_path / "p7.db")
    with open_db(ctx.db_path) as conn:
        create_jail(
            conn,
            guild_id=ctx.guild_id,
            user_id=42,
            moderator_id=1,
            reason="prev",
            stored_roles=[],
            channel_id=0,
            duration_seconds=3600,
        )

    guild = _guild(guild_id=ctx.guild_id)
    target = _member(42)
    moderator = _member(1)
    result = check_jail_preconditions(ctx, guild, target, moderator)
    assert result is not None
    assert result.error_kind == "already_jailed"


def test_precheck_returns_none_for_normal_member(tmp_path):
    """A regular member with a regular mod actor: precheck passes."""
    ctx = _make_ctx(tmp_path / "p8.db")
    guild = _guild()
    target = _member(42)
    moderator = _member(1, manage_guild=True)
    assert check_jail_preconditions(ctx, guild, target, moderator) is None


# ── apply_jail: happy path ───────────────────────────────────────────


async def test_apply_jail_persists_jail_and_audit_with_source(tmp_path):
    ctx = _make_ctx(tmp_path / "a1.db")
    guild = _guild(guild_id=ctx.guild_id)

    # Pre-configure an existing Jailed role so apply_jail doesn't need to
    # create one (that path is exercised separately).
    jailed_role = MagicMock(spec=discord.Role)
    jailed_role.id = 5000
    guild.roles.append(jailed_role)
    guild.get_role = MagicMock(side_effect=lambda rid: jailed_role if rid == 5000 else None)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    jail_channel = MagicMock(spec=discord.TextChannel)
    jail_channel.id = 6000
    jail_channel.send = AsyncMock()
    guild.create_text_channel = AsyncMock(return_value=jail_channel)

    target = _member(42, role_ids=(700, 701))
    moderator = _member(1)

    result = await apply_jail(
        ctx,
        guild,
        target,
        moderator,
        reason="test reason",
        duration_seconds=3600,
        source="dashboard",
    )

    assert result.ok is True
    assert result.jail_id is not None
    assert result.channel_id == 6000

    # Role was applied (target.edit called with the jailed role)
    target.edit.assert_awaited_once()
    call_kwargs = target.edit.call_args.kwargs
    assert call_kwargs["roles"] == [jailed_role]

    # jails row + audit_log row written, with source recorded
    with open_db(ctx.db_path) as conn:
        jail = conn.execute(
            "SELECT user_id, reason, channel_id FROM jails WHERE guild_id = ?",
            (ctx.guild_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT action, target_id, extra FROM audit_log"
            " WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (ctx.guild_id,),
        ).fetchone()
    assert jail["user_id"] == 42
    assert jail["reason"] == "test reason"
    assert jail["channel_id"] == 6000
    assert audit["action"] == "jail_create"
    import json
    extra = json.loads(audit["extra"])
    assert extra["source"] == "dashboard"
    assert extra["jail_id"] == result.jail_id

    # Welcome embed posted + DM sent
    jail_channel.send.assert_awaited()
    target.send.assert_awaited()


async def test_apply_jail_creates_jailed_role_when_missing(tmp_path):
    """If no jailed_role_id is configured, apply_jail creates the role and
    persists its ID via ctx.set_config_value."""
    ctx = _make_ctx(tmp_path / "a2.db")
    guild = _guild(guild_id=ctx.guild_id)

    new_role = MagicMock(spec=discord.Role)
    new_role.id = 9999
    guild.create_role = AsyncMock(return_value=new_role)

    jail_channel = MagicMock(spec=discord.TextChannel)
    jail_channel.id = 6000
    jail_channel.send = AsyncMock()
    guild.create_text_channel = AsyncMock(return_value=jail_channel)
    guild.channels = []  # no channels → set_permissions loop is empty

    target = _member(42)
    moderator = _member(1)

    result = await apply_jail(
        ctx, guild, target, moderator,
        reason="", duration_seconds=None, source="command",
    )

    assert result.ok is True
    guild.create_role.assert_awaited_once()

    # Newly-created role ID was persisted to config
    with open_db(ctx.db_path) as conn:
        from bot_modules.core.db_utils import get_config_value
        assert get_config_value(conn, "jailed_role_id", "0", ctx.guild_id) == "9999"


async def test_apply_jail_returns_no_role_perms_on_create_role_forbidden(tmp_path):
    ctx = _make_ctx(tmp_path / "a3.db")
    guild = _guild(guild_id=ctx.guild_id)

    forbidden = discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"),
        {"code": 50013, "message": "Missing Permissions"},
    )
    guild.create_role = AsyncMock(side_effect=forbidden)

    result = await apply_jail(
        ctx, guild, _member(42), _member(1),
        reason="", duration_seconds=None,
    )
    assert result.ok is False
    assert result.error_kind == "no_role_perms"


async def test_apply_jail_returns_no_member_perms_on_edit_forbidden(tmp_path):
    ctx = _make_ctx(tmp_path / "a4.db")
    guild = _guild(guild_id=ctx.guild_id)
    jailed_role = MagicMock(spec=discord.Role)
    jailed_role.id = 5000
    guild.get_role = MagicMock(return_value=jailed_role)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    target = _member(42)
    target.edit = AsyncMock(side_effect=discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"), {"code": 50013}
    ))

    result = await apply_jail(
        ctx, guild, target, _member(1),
        reason="", duration_seconds=None,
    )
    assert result.ok is False
    assert result.error_kind == "no_member_perms"


async def test_apply_jail_returns_no_channel_perms_on_create_channel_forbidden(tmp_path):
    """Critical: when channel creation fails, the role was already applied —
    the function must still return a clear error so the operator knows the
    user is jailed but the channel is missing."""
    ctx = _make_ctx(tmp_path / "a5.db")
    guild = _guild(guild_id=ctx.guild_id)
    jailed_role = MagicMock(spec=discord.Role)
    jailed_role.id = 5000
    guild.get_role = MagicMock(return_value=jailed_role)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    forbidden = discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"), {"code": 50013}
    )
    guild.create_text_channel = AsyncMock(side_effect=forbidden)

    target = _member(42)
    result = await apply_jail(
        ctx, guild, target, _member(1),
        reason="", duration_seconds=None,
    )
    assert result.ok is False
    assert result.error_kind == "no_channel_perms"
    # The role edit DID happen before the channel creation failed.
    target.edit.assert_awaited_once()


async def test_apply_jail_short_circuits_on_precheck_failure(tmp_path):
    """If preconditions fail, apply_jail returns the precheck failure
    without touching Discord."""
    ctx = _make_ctx(tmp_path / "a6.db")
    guild = _guild(guild_id=ctx.guild_id)

    bot_target = _member(42, is_bot=True)
    result = await apply_jail(
        ctx, guild, bot_target, _member(1), reason="", duration_seconds=None,
    )
    assert result.ok is False
    assert result.error_kind == "bot_target"
    # No Discord-side calls made
    guild.create_role.assert_not_called()
    bot_target.edit.assert_not_called()
    guild.create_text_channel.assert_not_called()


async def test_apply_jail_dm_failure_is_non_fatal(tmp_path):
    """A user with DMs closed → DM raises Forbidden, but the jail still
    succeeds. A fallback note is posted in the jail channel instead."""
    ctx = _make_ctx(tmp_path / "a7.db")
    guild = _guild(guild_id=ctx.guild_id)
    jailed_role = MagicMock(spec=discord.Role)
    jailed_role.id = 5000
    guild.get_role = MagicMock(return_value=jailed_role)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    jail_channel = MagicMock(spec=discord.TextChannel)
    jail_channel.id = 6000
    jail_channel.send = AsyncMock()
    guild.create_text_channel = AsyncMock(return_value=jail_channel)

    target = _member(42)
    target.send = AsyncMock(
        side_effect=discord.Forbidden(
            MagicMock(status=403, reason="Forbidden"), {"code": 50007}
        )
    )

    result = await apply_jail(
        ctx, guild, target, _member(1), reason="", duration_seconds=None,
    )
    assert result.ok is True
    # The jail channel got both the welcome embed and the "couldn't DM" fallback
    assert jail_channel.send.await_count >= 2


async def test_apply_jail_includes_mod_role_overwrites_in_channel(tmp_path):
    """Mod roles configured in guild_config get view+manage perms on the
    jail channel via the overwrites map."""
    ctx = _make_ctx(tmp_path / "a8.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "777,778", guild_id=ctx.guild_id)
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    guild = _guild(guild_id=ctx.guild_id)
    jailed_role = MagicMock(spec=discord.Role)
    jailed_role.id = 5000
    mod_role_777 = MagicMock(spec=discord.Role)
    mod_role_777.id = 777
    mod_role_778 = MagicMock(spec=discord.Role)
    mod_role_778.id = 778

    def _get_role(rid):
        return {5000: jailed_role, 777: mod_role_777, 778: mod_role_778}.get(rid)

    guild.get_role = MagicMock(side_effect=_get_role)

    jail_channel = MagicMock(spec=discord.TextChannel)
    jail_channel.id = 6000
    jail_channel.send = AsyncMock()
    guild.create_text_channel = AsyncMock(return_value=jail_channel)

    await apply_jail(
        ctx, guild, _member(42), _member(1),
        reason="", duration_seconds=None,
    )

    overwrites = guild.create_text_channel.call_args.kwargs["overwrites"]
    assert mod_role_777 in overwrites
    assert mod_role_778 in overwrites


# ── Indefinite vs timed jail ─────────────────────────────────────────


async def test_apply_jail_merges_source_extra_into_audit_row(tmp_path):
    """``source_extra`` keys land in the canonical audit row so callers like
    the dashboard ticket flow don't need a second cross-link audit entry."""
    ctx = _make_ctx(tmp_path / "ax.db")
    guild = _guild(guild_id=ctx.guild_id)
    jailed_role = MagicMock(spec=discord.Role)
    jailed_role.id = 5000
    guild.get_role = MagicMock(return_value=jailed_role)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    jail_channel = MagicMock(spec=discord.TextChannel)
    jail_channel.id = 6000
    jail_channel.send = AsyncMock()
    guild.create_text_channel = AsyncMock(return_value=jail_channel)

    result = await apply_jail(
        ctx, guild, _member(42), _member(1),
        reason="", duration_seconds=None,
        source="dashboard",
        source_extra={"ticket_id": 77, "ip_address": "10.0.0.1"},
    )
    assert result.ok is True

    with open_db(ctx.db_path) as conn:
        rows = conn.execute(
            "SELECT action, extra FROM audit_log WHERE guild_id = ?",
            (ctx.guild_id,),
        ).fetchall()
    # Exactly one audit row — no duplicate "cross-link" entry
    assert len(rows) == 1
    import json
    extra = json.loads(rows[0]["extra"])
    assert rows[0]["action"] == "jail_create"
    assert extra["ticket_id"] == 77
    assert extra["ip_address"] == "10.0.0.1"
    assert extra["source"] == "dashboard"


async def test_apply_jail_source_extra_cannot_overwrite_canonical_fields(tmp_path):
    """A caller can't silently spoof ``jail_id`` or ``source`` via source_extra."""
    ctx = _make_ctx(tmp_path / "ay.db")
    guild = _guild(guild_id=ctx.guild_id)
    jailed_role = MagicMock(spec=discord.Role)
    jailed_role.id = 5000
    guild.get_role = MagicMock(return_value=jailed_role)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    jail_channel = MagicMock(spec=discord.TextChannel)
    jail_channel.id = 6000
    jail_channel.send = AsyncMock()
    guild.create_text_channel = AsyncMock(return_value=jail_channel)

    result = await apply_jail(
        ctx, guild, _member(42), _member(1),
        reason="actual", duration_seconds=None,
        source="dashboard",
        source_extra={
            "jail_id": 9999999,
            "source": "haxx",
            "reason": "spoofed",
        },
    )
    assert result.ok is True

    with open_db(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT extra FROM audit_log WHERE guild_id = ?", (ctx.guild_id,),
        ).fetchone()
    import json
    extra = json.loads(row["extra"])
    assert extra["jail_id"] == result.jail_id  # canonical, not spoofed
    assert extra["source"] == "dashboard"  # not "haxx"
    assert extra["reason"] == "actual"  # not "spoofed"


async def test_apply_jail_indefinite_when_duration_none(tmp_path):
    ctx = _make_ctx(tmp_path / "a9.db")
    guild = _guild(guild_id=ctx.guild_id)
    jailed_role = MagicMock(spec=discord.Role)
    jailed_role.id = 5000
    guild.get_role = MagicMock(return_value=jailed_role)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "jailed_role_id", "5000", guild_id=ctx.guild_id)

    jail_channel = MagicMock(spec=discord.TextChannel)
    jail_channel.send = AsyncMock()
    jail_channel.id = 6000
    guild.create_text_channel = AsyncMock(return_value=jail_channel)

    result = await apply_jail(
        ctx, guild, _member(42), _member(1),
        reason="", duration_seconds=None,
    )
    assert result.ok is True

    with open_db(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT expires_at FROM jails WHERE id = ?", (result.jail_id,),
        ).fetchone()
    assert row["expires_at"] is None  # indefinite → no expiry
