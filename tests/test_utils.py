from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.utils import (
    format_guild_for_log,
    format_user_for_log,
    is_host_or_mod,
    is_mod_or_admin,
    safe_ephemeral,
    resolve_guild_for_log,
    resolve_user_for_log,
)


# ── format_user_for_log ───────────────────────────────────────────────

def test_no_args_returns_unknown():
    assert format_user_for_log() == "unknown user"


def test_user_id_only():
    assert format_user_for_log(user_id=42) == "user 42"


def test_display_name_matches_username():
    user = SimpleNamespace(id=1, display_name="Alice", name="Alice")
    assert format_user_for_log(user) == "Alice (1)"  # type: ignore[arg-type]


def test_display_name_differs_from_username():
    user = SimpleNamespace(id=1, display_name="Wonderland Alice", name="alice99")
    assert format_user_for_log(user) == "Wonderland Alice [alice99] (1)"  # type: ignore[arg-type]


def test_display_name_none_falls_back_to_username():
    user = SimpleNamespace(id=5, display_name=None, name="bob")
    assert format_user_for_log(user) == "bob (5)"  # type: ignore[arg-type]


def test_user_overrides_user_id():
    user = SimpleNamespace(id=10, display_name="Carol", name="Carol")
    assert format_user_for_log(user, user_id=99) == "Carol (10)"  # type: ignore[arg-type]


def test_user_with_no_id_uses_fallback_id():
    user = SimpleNamespace(display_name="Dave", name="dave")
    assert format_user_for_log(user, user_id=7) == "Dave [dave] (7)"  # type: ignore[arg-type]


# ── resolve_user_for_log ──────────────────────────────────────────────

def test_known_member_uses_format():
    member = SimpleNamespace(id=10, display_name="Eve", name="Eve")
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 10 else None)
    assert resolve_user_for_log(guild, 10) == "Eve (10)"  # type: ignore[arg-type]


def test_unknown_member_falls_back_to_id():
    guild = SimpleNamespace(get_member=lambda uid: None)
    assert resolve_user_for_log(guild, 99) == "user 99"  # type: ignore[arg-type]


def test_none_guild_falls_back_to_id():
    assert resolve_user_for_log(None, 42) == "user 42"


# ── format_guild_for_log ──────────────────────────────────────────────

def test_format_guild_no_args_returns_unknown():
    assert format_guild_for_log() == "unknown guild"


def test_format_guild_id_only():
    assert format_guild_for_log(guild_id=42) == "guild 42"


def test_format_guild_with_name():
    guild = SimpleNamespace(id=7, name="My Server")
    assert format_guild_for_log(guild) == "My Server (7)"  # type: ignore[arg-type]


def test_format_guild_without_name_uses_id():
    guild = SimpleNamespace(id=7, name=None)
    assert format_guild_for_log(guild) == "guild 7"  # type: ignore[arg-type]


# ── resolve_guild_for_log ─────────────────────────────────────────────

def test_resolve_known_guild_uses_format():
    guild = SimpleNamespace(id=10, name="Zone")
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == 10 else None)
    assert resolve_guild_for_log(bot, 10) == "Zone (10)"  # type: ignore[arg-type]


def test_resolve_unknown_guild_falls_back_to_id():
    bot = SimpleNamespace(get_guild=lambda gid: None)
    assert resolve_guild_for_log(bot, 99) == "guild 99"  # type: ignore[arg-type]


def test_resolve_none_bot_falls_back_to_id():
    assert resolve_guild_for_log(None, 42) == "guild 42"


# ── is_host_or_mod ────────────────────────────────────────────────────
#
# The gate on every game view's host-only control. One definition now backs
# all 29 former copies, so every branch is exercised here rather than in a
# per-game cog test. Deliberately narrower than AppContext.is_mod (which also
# honours guild-configured mod roles) — see the docstring on the helper.

HOST_ID = 777


def _member(user_id: int, *, administrator=False, manage_guild=False) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild_permissions = SimpleNamespace(
        administrator=administrator, manage_guild=manage_guild
    )
    return member


def _interaction(user, *, in_guild: bool = True) -> discord.Interaction:
    return SimpleNamespace(  # type: ignore[return-value]
        user=user, guild=SimpleNamespace(id=1) if in_guild else None
    )


def test_host_passes():
    assert is_host_or_mod(_interaction(_member(HOST_ID)), HOST_ID) is True


def test_host_passes_even_in_a_dm():
    """The host check runs before the guild check, so it holds with no guild."""
    user = MagicMock(spec=discord.User)
    user.id = HOST_ID
    assert is_host_or_mod(_interaction(user, in_guild=False), HOST_ID) is True


@pytest.mark.parametrize(
    "perms",
    [
        pytest.param({"administrator": True}, id="administrator"),
        pytest.param({"manage_guild": True}, id="manage_guild"),
        pytest.param({"administrator": True, "manage_guild": True}, id="both"),
    ],
)
def test_mod_overrides_the_host(perms):
    assert is_host_or_mod(_interaction(_member(1, **perms)), HOST_ID) is True


def test_plain_member_is_refused():
    assert is_host_or_mod(_interaction(_member(1)), HOST_ID) is False


@pytest.mark.parametrize(
    "perms",
    [
        pytest.param({"manage_channels": True}, id="manage_channels"),
        pytest.param({"manage_messages": True}, id="manage_messages"),
        pytest.param({"moderate_members": True}, id="moderate_members"),
    ],
)
def test_other_elevated_perms_do_not_qualify(perms):
    """Only administrator/manage_guild count.

    is_mod_or_admin — the /games admin-command gate — *does* accept
    manage_channels. Keeping that perm out here is the whole reason the two
    rules stayed separate; if this test starts failing, a gate was widened.
    """
    member = _member(1)
    member.guild_permissions = SimpleNamespace(
        administrator=False, manage_guild=False, **perms
    )
    assert is_host_or_mod(_interaction(member), HOST_ID) is False


def test_non_member_user_in_a_guild_is_refused():
    """A raw User (uncached member) has no guild_permissions to trust."""
    user = MagicMock(spec=discord.User)
    user.id = 1
    assert is_host_or_mod(_interaction(user), HOST_ID) is False


def test_mod_in_a_dm_is_refused():
    """No guild on the interaction ⇒ no guild permissions apply."""
    assert (
        is_host_or_mod(_interaction(_member(1, administrator=True), in_guild=False), HOST_ID)
        is False
    )


# ── safe_ephemeral ────────────────────────────────────────────────────
#
# Nine copies of this send-or-followup dance lived across economy/, casino/,
# services/ and confessions_cog. The branch that matters is which of the two
# send paths gets used: picking the wrong one raises inside a button callback,
# which the member sees as "This interaction failed".


def _send_interaction(*, responded: bool, raises: Exception | None = None):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = responded
    interaction.response.send_message = AsyncMock(side_effect=raises)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock(side_effect=raises)
    return interaction


@pytest.mark.asyncio
async def test_fresh_interaction_replies_through_response():
    interaction = _send_interaction(responded=False)
    await safe_ephemeral(interaction, "hi")
    interaction.response.send_message.assert_awaited_once_with("hi", ephemeral=True)
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_responded_interaction_replies_through_followup():
    """Deferred, or a modal answered first — ``response`` is spent."""
    interaction = _send_interaction(responded=True)
    await safe_ephemeral(interaction, "hi")
    interaction.followup.send.assert_awaited_once_with("hi", ephemeral=True)
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("responded", [False, True], ids=["response", "followup"])
async def test_http_failure_is_swallowed(responded):
    """Best-effort: a raise here would surface as an interaction failure."""
    boom = discord.HTTPException(MagicMock(status=500), "nope")
    await safe_ephemeral(_send_interaction(responded=responded, raises=boom), "hi")


@pytest.mark.asyncio
async def test_non_http_errors_still_propagate():
    """Only HTTPException is swallowed — a bug in the caller shouldn't vanish."""
    interaction = _send_interaction(responded=False, raises=ValueError("bug"))
    with pytest.raises(ValueError):
        await safe_ephemeral(interaction, "hi")


@pytest.mark.asyncio
async def test_log_label_names_the_caller(caplog):
    """Each module binds its own label; the traceback alone points here."""
    boom = discord.HTTPException(MagicMock(status=500), "nope")
    with caplog.at_level(logging.DEBUG, logger="bot_modules.core.utils"):
        await safe_ephemeral(
            _send_interaction(responded=False, raises=boom), "hi", log_label="econ pin"
        )
    assert "econ pin" in caplog.text


@pytest.mark.asyncio
async def test_module_partials_carry_their_label():
    """The nine former copies are now one function plus a bound label."""
    from bot_modules.economy.pin_views import _safe_ephemeral

    interaction = _send_interaction(responded=False)
    await _safe_ephemeral(interaction, "hi")
    interaction.response.send_message.assert_awaited_once_with("hi", ephemeral=True)


# ── is_mod_or_admin ───────────────────────────────────────────────────
#
# The /games admin-command gate, formerly duplicated in games_config_cog and
# games_external_cog. Wider than is_host_or_mod: manage_channels qualifies
# here and deliberately does not there.


def _predicate():
    """Pull the check out of the decorator so it can be called directly."""

    @is_mod_or_admin()
    async def command(interaction):  # pragma: no cover - never invoked
        ...

    (check,) = command.__discord_app_commands_checks__
    return check


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "perms",
    [
        pytest.param({"administrator": True}, id="administrator"),
        pytest.param({"manage_guild": True}, id="manage_guild"),
        pytest.param({"manage_channels": True}, id="manage_channels"),
    ],
)
async def test_each_elevated_perm_qualifies(perms):
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(
        **{"administrator": False, "manage_guild": False, "manage_channels": False, **perms}
    )
    assert await _predicate()(_interaction(member)) is True


@pytest.mark.asyncio
async def test_manage_channels_qualifies_here_but_not_for_a_game_host():
    """The one perm that separates this gate from is_host_or_mod.

    Both rules have one definition now; this pins the difference between
    them so a later tidy-up can't quietly merge them.
    """
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(
        administrator=False, manage_guild=False, manage_channels=True
    )
    interaction = _interaction(member)
    assert await _predicate()(interaction) is True
    assert is_host_or_mod(interaction, HOST_ID) is False


@pytest.mark.asyncio
async def test_plain_member_is_refused_by_the_admin_gate():
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(
        administrator=False, manage_guild=False, manage_channels=False
    )
    assert await _predicate()(_interaction(member)) is False


@pytest.mark.asyncio
async def test_refused_outside_a_guild_and_for_a_non_member():
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(administrator=True, manage_guild=True)
    assert await _predicate()(_interaction(member, in_guild=False)) is False

    user = MagicMock(spec=discord.User)
    assert await _predicate()(_interaction(user)) is False
