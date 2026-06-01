"""Tests for bot_modules.commands.jail_commands helper functions.

Focused on the prelaunch P0 fix: ``_get_mod_role_ids``/``_get_admin_role_ids``
take an explicit ``guild_id``, and ``_is_mod``/``_is_admin`` look up roles
via ``ctx.guild_config(member.guild.id)`` rather than the home-guild flat
fields on ``AppContext``. Without this scoping, a 2nd guild would silently
inherit the home guild's mod/admin role list.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from bot_modules.commands.jail_commands import (
    _get_admin_role_ids,
    _get_config,
    _get_mod_role_ids,
    _is_admin,
    _is_mod,
)
from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import open_db, set_config_value as _db_set
from migrations import apply_migrations_sync


def _make_ctx(db_path, guild_id: int = 10) -> AppContext:
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
