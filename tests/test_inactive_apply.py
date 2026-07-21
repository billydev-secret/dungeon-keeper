"""Tests for bot_modules.inactive.apply and .store — the destructive core.

The sweep's *selection* is covered by test_inactive_logic.py. This file covers
the part where roles actually get stripped and given back: the snapshot/strip in
``apply_inactive``, the restore in ``reactivate_member``, and the DB round-trip
in ``store`` that carries ``stored_roles`` between them. A bug here is silent,
permanent role loss, so the round-trip is asserted end to end.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import discord

from bot_modules.core.app_context import AppContext
from bot_modules.inactive.apply import (
    apply_inactive,
    check_inactive_preconditions,
    reactivate_member,
)
from bot_modules.inactive.store import (
    active_inactive_user_ids,
    create_inactive,
    get_active_inactive,
    reactivate_inactive,
)
from migrations import apply_migrations_sync

INACTIVE_ROLE_ID = 555


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


def _role(role_id: int, *, managed: bool = False) -> MagicMock:
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    r.managed = managed
    r.name = f"role{role_id}"
    return r


def _member(
    member_id: int,
    *,
    is_bot: bool = False,
    admin: bool = False,
    manage_guild: bool = False,
    role_ids: tuple[int, ...] = (),
) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = is_bot
    m.name = f"u{member_id}"
    m.display_name = m.name
    m.mention = f"<@{member_id}>"
    perms = MagicMock()
    perms.administrator = admin
    perms.manage_guild = manage_guild
    m.guild_permissions = perms
    m.roles = [_role(rid) for rid in role_ids]
    m.remove_roles = AsyncMock()
    m.add_roles = AsyncMock()
    m.send = AsyncMock()
    return m


def _guild(*, guild_id: int = 100, members: list | None = None) -> MagicMock:
    members = members or []
    by_id = {m.id: m for m in members}
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    g.name = "Test Guild"
    g.get_member = MagicMock(side_effect=lambda uid: by_id.get(int(uid)))
    default_role = _role(0)
    g.default_role = default_role
    # The @Inactive role already exists so ensure_inactive_role short-circuits
    # (no create_role / channel iteration needed in these tests).
    inactive_role = _role(INACTIVE_ROLE_ID)
    g.roles = [default_role, inactive_role, _role(11), _role(12), _role(13)]
    g.get_role = MagicMock(side_effect=lambda rid: {r.id: r for r in g.roles}.get(rid))
    g.get_channel = MagicMock(return_value=None)
    g.create_role = AsyncMock()
    g.me = _member(99)
    return g


def _configure_inactive_role(ctx: AppContext, guild_id: int) -> None:
    ctx.set_config_value("inactive_role_id", str(INACTIVE_ROLE_ID), guild_id)


# ── Preconditions ────────────────────────────────────────────────────


def test_precheck_rejects_bot(tmp_path):
    ctx = _make_ctx(tmp_path / "a.db")
    guild = _guild()
    result = check_inactive_preconditions(ctx, guild, _member(1, is_bot=True), _member(2))
    assert result is not None and result.error_kind == "bot_target"


def test_precheck_rejects_self(tmp_path):
    ctx = _make_ctx(tmp_path / "b.db")
    me = _member(5)
    result = check_inactive_preconditions(ctx, _guild(), me, me)
    assert result is not None and result.error_kind == "self_target"


def test_precheck_rejects_admin_target(tmp_path):
    ctx = _make_ctx(tmp_path / "c.db")
    result = check_inactive_preconditions(ctx, _guild(), _member(5, admin=True), _member(2))
    assert result is not None and result.error_kind == "admin_target"


def test_precheck_rejects_mod_target_for_non_admin(tmp_path):
    ctx = _make_ctx(tmp_path / "d.db")
    target = _member(5, manage_guild=True)  # mod via manage_guild
    result = check_inactive_preconditions(ctx, _guild(), target, _member(2))
    assert result is not None and result.error_kind == "mod_target"


def test_precheck_rejects_already_inactive(tmp_path):
    ctx = _make_ctx(tmp_path / "e.db")
    guild = _guild()
    with ctx.open_db() as conn:
        create_inactive(
            conn, guild_id=guild.id, user_id=5, moderator_id=2,
            reason="", stored_roles=[11], source="command",
        )
    result = check_inactive_preconditions(ctx, guild, _member(5), _member(2))
    assert result is not None and result.error_kind == "already_inactive"


def test_precheck_ok_returns_none(tmp_path):
    ctx = _make_ctx(tmp_path / "f.db")
    assert check_inactive_preconditions(ctx, _guild(), _member(5, role_ids=(11,)), _member(2)) is None


# ── apply_inactive: snapshot + strip ─────────────────────────────────


async def test_apply_snapshots_and_strips_roles(tmp_path):
    ctx = _make_ctx(tmp_path / "g.db")
    _configure_inactive_role(ctx, 100)
    guild = _guild()
    target = _member(5, role_ids=(11, 12, 13))
    mod = _member(2)

    outcome = await apply_inactive(ctx, guild, target, mod, reason="idle")
    assert outcome.ok

    # Real roles stripped, @Inactive added.
    stripped = {r.id for r in target.remove_roles.call_args.args}
    assert stripped == {11, 12, 13}
    assert target.add_roles.call_args.args[0].id == INACTIVE_ROLE_ID

    # Snapshot persisted (excludes @everyone and the Inactive role).
    with ctx.open_db() as conn:
        row = get_active_inactive(conn, 100, 5)
    assert row is not None
    assert json.loads(row["stored_roles"]) == [11, 12, 13]


async def test_apply_is_idempotent(tmp_path):
    ctx = _make_ctx(tmp_path / "h.db")
    _configure_inactive_role(ctx, 100)
    guild = _guild()
    target = _member(5, role_ids=(11,))
    first = await apply_inactive(ctx, guild, target, _member(2))
    assert first.ok
    second = await apply_inactive(ctx, guild, target, _member(2))
    assert not second.ok and second.error_kind == "already_inactive"


# ── Round-trip: apply then reactivate restores the exact role set ────


async def test_roundtrip_restores_exact_roles(tmp_path):
    ctx = _make_ctx(tmp_path / "i.db")
    _configure_inactive_role(ctx, 100)
    guild = _guild()
    target = _member(5, role_ids=(11, 12, 13))
    mod = _member(2)

    await apply_inactive(ctx, guild, target, mod)

    # Simulate Discord state after the strip: member now holds only @Inactive.
    target.roles = [guild.default_role, guild.get_role(INACTIVE_ROLE_ID)]

    msg = await reactivate_member(ctx, guild, target, reason="back", actor=mod)
    assert msg.startswith("✅")

    restored = {r.id for r in target.add_roles.call_args.args}
    assert restored == {11, 12, 13}
    assert target.remove_roles.call_args.args[0].id == INACTIVE_ROLE_ID

    # Row flipped to reactivated; no longer active.
    with ctx.open_db() as conn:
        assert get_active_inactive(conn, 100, 5) is None


async def test_reactivate_notes_deleted_roles(tmp_path):
    ctx = _make_ctx(tmp_path / "j.db")
    _configure_inactive_role(ctx, 100)
    guild = _guild()
    target = _member(5, role_ids=(11, 999))  # 999 will be "deleted" (not in guild.roles)
    await apply_inactive(ctx, guild, target, _member(2))
    target.roles = [guild.default_role, guild.get_role(INACTIVE_ROLE_ID)]

    msg = await reactivate_member(ctx, guild, target, reason="back")
    # Only role 11 still exists; 999 is missing.
    restored = {r.id for r in target.add_roles.call_args.args}
    assert restored == {11}
    assert "Could not restore" in msg


async def test_reactivate_when_not_inactive(tmp_path):
    ctx = _make_ctx(tmp_path / "k.db")
    guild = _guild()
    msg = await reactivate_member(ctx, guild, _member(5), reason="x")
    assert "not currently in the inactive channel" in msg


# ── store round-trip ─────────────────────────────────────────────────


def test_store_lifecycle(tmp_path):
    ctx = _make_ctx(tmp_path / "l.db")
    with ctx.open_db() as conn:
        iid = create_inactive(
            conn, guild_id=100, user_id=5, moderator_id=2,
            reason="idle", stored_roles=[11, 12], source="auto",
        )
        assert active_inactive_user_ids(conn, 100) == {5}
        row = get_active_inactive(conn, 100, 5)
        assert row is not None and row["source"] == "auto"
        assert json.loads(row["stored_roles"]) == [11, 12]

        assert reactivate_inactive(conn, iid, reason="back") is True
        assert active_inactive_user_ids(conn, 100) == set()
        assert get_active_inactive(conn, 100, 5) is None
        # Second reactivate is a no-op (already not active).
        assert reactivate_inactive(conn, iid, reason="again") is False
