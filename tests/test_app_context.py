from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot_modules.core.app_context import AppContext, Bot, GuildConfig
from bot_modules.core.db_utils import open_db, set_config_value as _db_set
from bot_modules.core.xp_system import DEFAULT_XP_SETTINGS
from bot_modules.services.welcome_service import (
    DEFAULT_LEAVE_MESSAGE,
    DEFAULT_WELCOME_MESSAGE,
)
from migrations import apply_migrations_sync


def _make_ctx(db_path, guild_id: int = 123) -> AppContext:
    """Construct a minimal AppContext backed by a real (migrated) temp DB."""
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


def test_set_config_value_invalidates_guild_config_cache(tmp_path):
    """A bot-side config write must refresh the per-guild snapshot, not just flats."""
    ctx = _make_ctx(tmp_path / "ctx.db")

    # Prime the cache with the empty snapshot.
    assert ctx.guild_config(ctx.guild_id).mod_role_ids == frozenset()

    # Write mod roles the way an in-Discord setup flow does.
    ctx.set_config_value("mod_role_ids", "900,901")

    # The next read must reflect the write (cache was invalidated), and agree
    # with the flat cache that set_config_value maintains.
    assert ctx.guild_config(ctx.guild_id).mod_role_ids == frozenset({900, 901})
    assert ctx.mod_role_ids == {900, 901}


def test_delete_config_value_invalidates_guild_config_cache(tmp_path):
    ctx = _make_ctx(tmp_path / "ctx2.db")
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "900", ctx.guild_id)

    assert ctx.guild_config(ctx.guild_id).mod_role_ids == frozenset({900})
    ctx.delete_config_value("mod_role_ids")
    assert ctx.guild_config(ctx.guild_id).mod_role_ids == frozenset()


def test_bucket_mutators_invalidate_guild_config_cache(tmp_path):
    """add/remove/clear config-id bucket writes must refresh the per-guild snapshot."""
    ctx = _make_ctx(tmp_path / "ctx_bucket.db")

    # Prime cache (empty), then add a spoiler channel via the ctx mutator.
    assert ctx.guild_config(ctx.guild_id).spoiler_required_channels == frozenset()
    ctx.add_config_id_value("spoiler_required_channels", 555)
    assert ctx.guild_config(ctx.guild_id).spoiler_required_channels == frozenset({555})

    ctx.remove_config_id_value("spoiler_required_channels", 555)
    assert ctx.guild_config(ctx.guild_id).spoiler_required_channels == frozenset()

    ctx.add_config_id_value("spoiler_required_channels", 777)
    ctx.clear_config_id_bucket("spoiler_required_channels")
    assert ctx.guild_config(ctx.guild_id).spoiler_required_channels == frozenset()


# ── GuildConfig.load ──────────────────────────────────────────────────


def test_guild_config_load_returns_defaults_when_unconfigured(tmp_path):
    db_path = tmp_path / "gc1.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as conn:
        cfg = GuildConfig.load(conn, guild_id=999, allow_legacy_fallback=False)

    assert cfg.guild_id == 999
    assert cfg.welcome_channel_id == 0
    assert cfg.welcome_message == DEFAULT_WELCOME_MESSAGE
    assert cfg.leave_message == DEFAULT_LEAVE_MESSAGE
    assert cfg.mod_role_ids == frozenset()
    assert cfg.admin_role_ids == frozenset()


def test_guild_config_load_reads_guild_specific_values(tmp_path):
    db_path = tmp_path / "gc2.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as conn:
        _db_set(conn, "welcome_channel_id", "111", guild_id=42)
        _db_set(conn, "welcome_message", "hi {mention}", guild_id=42)
        _db_set(conn, "welcome_ping_role_id", "222", guild_id=42)
        _db_set(conn, "leave_channel_id", "333", guild_id=42)
        _db_set(conn, "leave_message", "bye {name}", guild_id=42)
        _db_set(conn, "mod_role_ids", "500,501", guild_id=42)
        _db_set(conn, "admin_role_ids", "600,601", guild_id=42)
        _db_set(conn, "mod_channel_id", "777", guild_id=42)

        cfg = GuildConfig.load(conn, guild_id=42, allow_legacy_fallback=False)

    assert cfg.welcome_channel_id == 111
    assert cfg.welcome_message == "hi {mention}"
    assert cfg.welcome_ping_role_id == 222
    assert cfg.leave_channel_id == 333
    assert cfg.leave_message == "bye {name}"
    assert cfg.mod_role_ids == frozenset({500, 501})
    assert cfg.admin_role_ids == frozenset({600, 601})
    assert cfg.mod_channel_id == 777


def test_guild_config_load_strict_mode_ignores_legacy(tmp_path):
    """A non-home guild with no rows must NOT inherit legacy guild_id=0 config."""
    db_path = tmp_path / "gc3.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as conn:
        _db_set(conn, "welcome_channel_id", "999", guild_id=0)  # legacy
        _db_set(conn, "mod_role_ids", "1,2,3", guild_id=0)

        cfg = GuildConfig.load(conn, guild_id=42, allow_legacy_fallback=False)

    assert cfg.welcome_channel_id == 0
    assert cfg.mod_role_ids == frozenset()


def test_guild_config_load_home_guild_uses_legacy_fallback(tmp_path):
    """Home guild reads legacy rows when its own rows aren't present."""
    db_path = tmp_path / "gc4.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as conn:
        _db_set(conn, "welcome_channel_id", "888", guild_id=0)  # legacy only

        cfg = GuildConfig.load(conn, guild_id=42, allow_legacy_fallback=True)

    assert cfg.welcome_channel_id == 888


def test_guild_config_load_join_leave_log_defaults_to_leave_channel(tmp_path):
    db_path = tmp_path / "gc5.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as conn:
        _db_set(conn, "leave_channel_id", "55", guild_id=42)
        cfg = GuildConfig.load(conn, guild_id=42, allow_legacy_fallback=False)
    assert cfg.join_leave_log_channel_id == 55


def test_guild_config_member_is_mod_matches_mod_or_admin_role():
    cfg = GuildConfig(
        guild_id=1,
        welcome_channel_id=0,
        welcome_message="",
        welcome_ping_role_id=0,
        greeter_chat_channel_id=0,
        leave_channel_id=0,
        leave_message="",
        join_leave_log_channel_id=0,
        mod_channel_id=0,
        mod_role_ids=frozenset({10}),
        admin_role_ids=frozenset({20}),
        spoiler_required_channels=frozenset(),
        bypass_role_ids=frozenset(),
        recorded_bot_user_ids=frozenset(),
        xp_excluded_channel_ids=frozenset(),
        xp_grant_allowed_user_ids=frozenset(),
        level_5_role_id=0,
        level_5_log_channel_id=0,
        level_up_log_channel_id=0,
        xp_settings=DEFAULT_XP_SETTINGS,
        grant_roles={},
    )

    def _member_with_role_ids(*role_ids: int) -> MagicMock:
        m = MagicMock()
        m.roles = [MagicMock(id=rid) for rid in role_ids]
        return m

    assert cfg.member_is_mod(_member_with_role_ids(10)) is True
    assert cfg.member_is_mod(_member_with_role_ids(20)) is True  # admin counts
    assert cfg.member_is_mod(_member_with_role_ids(99)) is False
    assert cfg.member_is_admin(_member_with_role_ids(20)) is True
    assert cfg.member_is_admin(_member_with_role_ids(10)) is False  # mod ≠ admin


# ── AppContext.guild_config caching ───────────────────────────────────


def test_guild_config_is_cached_per_guild(tmp_path):
    ctx = _make_ctx(tmp_path / "ctx_cache.db")
    cfg_a = ctx.guild_config(ctx.guild_id)
    cfg_a_again = ctx.guild_config(ctx.guild_id)
    assert cfg_a is cfg_a_again  # same object → DB only read once


def test_guild_config_caches_distinct_guilds_independently(tmp_path):
    ctx = _make_ctx(tmp_path / "ctx_distinct.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "welcome_channel_id", "111", guild_id=10)
        _db_set(conn, "welcome_channel_id", "222", guild_id=20)

    assert ctx.guild_config(10).welcome_channel_id == 111
    assert ctx.guild_config(20).welcome_channel_id == 222


def test_guild_config_home_vs_non_home_legacy_fallback(tmp_path):
    """guild_config picks strict mode for non-home guilds, fallback for home."""
    ctx = _make_ctx(tmp_path / "ctx_strict.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "welcome_channel_id", "777", guild_id=0)  # legacy only

    assert ctx.guild_config(10).welcome_channel_id == 777  # home → fallback used
    assert ctx.guild_config(20).welcome_channel_id == 0  # other → strict


def test_invalidate_guild_config_drops_only_target_guild(tmp_path):
    ctx = _make_ctx(tmp_path / "ctx_inv.db", guild_id=10)
    cfg_10 = ctx.guild_config(10)
    cfg_20 = ctx.guild_config(20)

    ctx.invalidate_guild_config(10)

    # 10 must reload (new instance), 20 must stay cached (same instance).
    assert ctx.guild_config(10) is not cfg_10
    assert ctx.guild_config(20) is cfg_20


def test_reload_permission_roles_scoped_to_home_guild(tmp_path):
    """reload_permission_roles must NOT pick up another guild's role IDs."""
    ctx = _make_ctx(tmp_path / "ctx_reload.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "100,101", guild_id=10)
        _db_set(conn, "mod_role_ids", "200,201", guild_id=20)

    ctx.reload_permission_roles()
    assert ctx.mod_role_ids == {100, 101}


async def test_is_mod_uses_per_guild_config(tmp_path):
    """is_mod must read mod roles for the interaction's guild, not just home."""
    ctx = _make_ctx(tmp_path / "ctx_ismod.db", guild_id=10)
    with open_db(ctx.db_path) as conn:
        _db_set(conn, "mod_role_ids", "555", guild_id=20)  # other guild only

    # Member with role 555 in guild 20 → is_mod true
    member = MagicMock()
    member.roles = [MagicMock(id=555)]

    ix = MagicMock(spec=discord.Interaction)
    ix.guild_id = 20
    ix.permissions = MagicMock(manage_guild=False, administrator=False)
    ix.guild = MagicMock()
    ix.guild.get_member = MagicMock(return_value=member)
    ix.user = member

    assert ctx.is_mod(ix) is True

    # Different member with no relevant role → is_mod false
    other = MagicMock()
    other.roles = [MagicMock(id=999)]
    ix.guild.get_member = MagicMock(return_value=other)
    ix.user = other
    assert ctx.is_mod(ix) is False


async def test_is_mod_returns_false_for_dm_interaction(tmp_path):
    """No guild_id → can't resolve per-guild config → not a mod."""
    ctx = _make_ctx(tmp_path / "ctx_dm.db", guild_id=10)
    member = MagicMock()
    member.roles = []

    ix = MagicMock(spec=discord.Interaction)
    ix.guild_id = None
    ix.permissions = MagicMock(manage_guild=False, administrator=False)
    ix.guild = None
    ix.user = member

    assert ctx.is_mod(ix) is False
    assert ctx.is_admin(ix) is False


async def test_setup_hook_skips_sync_for_non_positive_guild_id():
    bot = Bot(intents=discord.Intents.none(), debug=True, guild_id=0)
    bot.tree.sync = AsyncMock()
    with patch("builtins.print") as print_mock:
        await bot.setup_hook()
        await bot.close()

    bot.tree.sync.assert_not_called()
    assert print_mock.called
    assert "skipping guild command sync" in print_mock.call_args_list[0][0][0].lower()


async def test_setup_hook_handles_forbidden_during_debug_guild_sync(tmp_path):
    bot = Bot(intents=discord.Intents.none(), debug=True, guild_id=123)
    bot.ctx = MagicMock()
    bot.ctx.db_path = tmp_path / "test.db"
    forbidden = discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"),
        {"code": 50001, "message": "Missing Access"},
    )
    bot.tree.sync = AsyncMock(side_effect=forbidden)

    async def fake_sync_if_changed(tree, _db_path, *, guild):
        # Mirror real behaviour: call tree.sync, propagate Forbidden upward.
        if guild is None:
            await tree.sync()
        else:
            await tree.sync(guild=guild)
        return [], True

    with patch(
        "bot_modules.services.command_sync.sync_if_changed",
        side_effect=fake_sync_if_changed,
    ), patch("builtins.print") as print_mock:
        await bot.setup_hook()
        await bot.close()

    bot.tree.sync.assert_called_once()
    printed_text = "\n".join(
        str(call.args[0]) for call in print_mock.call_args_list if call.args
    )
    assert "missing access" in printed_text.lower()
