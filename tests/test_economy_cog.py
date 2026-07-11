"""Cog-level tests for /bank — wallet view and the mod grant permission matrix."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import (
    apply_credit,
    get_balance,
    save_econ_settings,
)
from migrations import apply_migrations_sync
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
MANAGER_ROLE_ID = 7007


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db_path=db, open_db=lambda: open_db(db))


@pytest.fixture(autouse=True)
def _patch_accent():
    """resolve_accent_color reads the guild avatar — stub it to a fixed colour."""
    with patch(
        "bot_modules.cogs.economy_cog.resolve_accent_color",
        new=AsyncMock(return_value=discord.Colour(0x123456)),
    ):
        yield


def _make_cog(ctx):
    from bot_modules.cogs.economy_cog import EconomyCog

    return EconomyCog(MagicMock(), ctx)


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True}
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _member(
    *,
    admin: bool = False,
    role_ids: tuple[int, ...] = (),
    member_id: int = 500,
    is_bot: bool = False,
    premium: object | None = None,
    name: str = "Actor",
) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = is_bot
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.premium_since = premium
    m.guild_permissions = MagicMock(administrator=admin)
    m.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return m


def _interaction(actor: MagicMock) -> MagicMock:
    inter = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    inter.user = actor
    return inter


async def _wallet(cog, interaction) -> None:
    await cog.bank_wallet.callback(cog, interaction)


async def _grant(cog, interaction, member, amount, reason) -> None:
    await cog.bank_grant.callback(cog, interaction, member, amount, reason)


# ── /bank wallet ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wallet_shows_balance_branding_and_ledger(ctx, db):
    _enable(db, currency_emoji="💎", currency_plural="Gems", wallet_name="Vault")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 30, "grant", actor_id=1, meta={"reason": "x"})

    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)

    await _wallet(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert embed.title == "Vault"
    assert "30" in embed.description and "Gems" in embed.description
    assert "💎" in embed.description
    activity = embed.fields[0]
    assert "grant" in activity.value and "+30" in activity.value


@pytest.mark.asyncio
async def test_wallet_disabled_gate(ctx, db):
    cog = _make_cog(ctx)  # economy left disabled
    interaction = _interaction(_member(member_id=500))

    await _wallet(cog, interaction)

    args = interaction.response.send_message.await_args.args
    kwargs = interaction.response.send_message.await_args.kwargs
    assert "enabled" in args[0].lower()
    assert kwargs["ephemeral"] is True
    assert "embed" not in kwargs


# ── /bank grant — permission matrix ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_grant_admin_allowed(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(admin=True)
    target = _member(member_id=900, name="Target")
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 25, "good work")

    kwargs = interaction.response.send_message.await_args.kwargs
    assert "embed" in kwargs
    assert kwargs.get("ephemeral") is not True  # public confirmation
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 25


@pytest.mark.asyncio
async def test_grant_manager_role_allowed(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE_ID)
    cog = _make_cog(ctx)
    actor = _member(admin=False, role_ids=(MANAGER_ROLE_ID,))
    target = _member(member_id=900)
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 10, "for helping")

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 10


@pytest.mark.asyncio
async def test_grant_plain_member_refused(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE_ID)
    cog = _make_cog(ctx)
    actor = _member(admin=False, role_ids=())  # no admin, no manager role
    target = _member(member_id=900)
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 10, "nope")

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "permission" in interaction.response.send_message.await_args.args[0].lower()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 0


@pytest.mark.asyncio
async def test_grant_disabled_gate(ctx, db):
    cog = _make_cog(ctx)  # disabled; caller is admin so only the gate can block
    actor = _member(admin=True)
    target = _member(member_id=900)
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 10, "x")

    args = interaction.response.send_message.await_args.args
    assert "enabled" in args[0].lower()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 0


# ── /bank grant — amounts and booster multiplier ─────────────────────────────


@pytest.mark.asyncio
async def test_grant_booster_target_gets_multiplier(ctx, db):
    _enable(db)  # default booster_multiplier == 1.5
    cog = _make_cog(ctx)
    actor = _member(admin=True)
    target = _member(member_id=900, premium=object())  # boosting
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 5, "boost love")

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 8  # ceil(5 * 1.5)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert any("Booster" in f.name for f in embed.fields)


@pytest.mark.asyncio
async def test_grant_rejects_amount_below_one(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(admin=True)
    target = _member(member_id=900)
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 0, "zero")

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "at least 1" in interaction.response.send_message.await_args.args[0].lower()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 0


@pytest.mark.asyncio
async def test_grant_rejects_bot_target(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(admin=True)
    target = _member(member_id=900, is_bot=True)
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 10, "bot")

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "bot" in interaction.response.send_message.await_args.args[0].lower()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 0
