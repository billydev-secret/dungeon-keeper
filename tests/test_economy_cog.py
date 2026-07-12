"""Cog-level tests for /bank — wallet view, mod grant matrix, and /bank quests."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quests import quest_period
from bot_modules.services.economy_quests_service import (
    claim_quest,
    create_quest,
    set_quest_active,
)
from bot_modules.services.economy_service import (
    apply_credit,
    get_balance,
    get_notify_muted,
    load_econ_settings,
    notify_member,
    save_econ_settings,
)
from bot_modules.services.quote_renderer import THEMES
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


# ── /qotd post ────────────────────────────────────────────────────────────────


def _qotd_interaction(actor: MagicMock) -> tuple[MagicMock, MagicMock]:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 12345
    posted = MagicMock()
    posted.id = 67890
    channel.send = AsyncMock(return_value=posted)
    inter = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    inter.user = actor
    inter.channel = channel
    return inter, channel


async def _qotd(cog, interaction, question) -> None:
    await cog.qotd_post.callback(cog, interaction, question)


@pytest.fixture(autouse=True)
def _patch_qotd_image():
    """Force the plain-embed fallback (no PIL render) in cog tests."""
    with patch(
        "bot_modules.cogs.economy_cog._resolve_qotd_image",
        new=AsyncMock(return_value=None),
    ):
        yield


@pytest.mark.asyncio
async def test_qotd_disabled_gate(ctx, db):
    cog = _make_cog(ctx)  # economy disabled
    interaction, channel = _qotd_interaction(_member(admin=True))
    await _qotd(cog, interaction, "What's your favorite game?")
    args = interaction.response.send_message.await_args.args
    assert "enabled" in args[0].lower()
    channel.send.assert_not_called()
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_qotd_admin_posts_and_records(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    await _qotd(cog, interaction, "What's your favorite game?")

    channel.send.assert_awaited_once()
    assert "embed" in channel.send.await_args.kwargs  # fell back to a branded embed
    interaction.followup.send.assert_awaited_once()
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT channel_id, message_id, question FROM econ_qotd"
        ).fetchone()
    assert row["channel_id"] == 12345
    assert row["message_id"] == 67890
    assert row["question"] == "What's your favorite game?"


@pytest.mark.asyncio
async def test_qotd_renders_card_when_image_available(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    with (
        patch(
            "bot_modules.cogs.economy_cog._resolve_qotd_image",
            new=AsyncMock(return_value=b"img-bytes"),
        ),
        patch(
            "bot_modules.cogs.economy_cog.render_quote_card", return_value=b"PNG"
        ) as mock_render,
    ):
        await _qotd(cog, interaction, "Card question?")

    mock_render.assert_called_once()
    kwargs = mock_render.call_args.kwargs
    assert kwargs["author_name"] == "Question of the Day"
    assert kwargs["pfp_shape"] == "none"
    assert kwargs["theme"] is THEMES["midnight"]
    # The rendered card is posted as a file attachment, not the embed fallback.
    assert "file" in channel.send.await_args.kwargs
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd").fetchone()["c"] == 1


@pytest.mark.asyncio
async def test_qotd_manager_role_allowed(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE_ID)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(
        _member(admin=False, role_ids=(MANAGER_ROLE_ID,))
    )
    await _qotd(cog, interaction, "Coffee or tea?")
    channel.send.assert_awaited_once()
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd").fetchone()["c"] == 1


@pytest.mark.asyncio
async def test_qotd_plain_member_refused(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE_ID)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=False, role_ids=()))
    await _qotd(cog, interaction, "Nope?")
    args = interaction.response.send_message.await_args.args
    assert "permission" in args[0].lower()
    channel.send.assert_not_called()
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM econ_qotd").fetchone()["c"] == 0


# ── /bank quests — listing state matrix ──────────────────────────────────────


def _mk_quest(
    db,
    *,
    qtype="daily",
    reward=15,
    signoff=0,
    community_target=None,
    active=True,
    title="Quest",
) -> int:
    with open_db(db) as conn:
        qid = create_quest(
            conn,
            GUILD_ID,
            title=title,
            description="",
            qtype=qtype,
            reward=reward,
            signoff=signoff,
            criteria="Do the thing",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=community_target,
            created_by=1,
        )
        if active:
            set_quest_active(conn, GUILD_ID, qid, True)
    return qid


def _period(db, qtype) -> str:
    with open_db(db) as conn:
        offset = get_tz_offset_hours(conn, GUILD_ID)
    return quest_period(qtype, local_day_for(time.time(), offset))


async def _quests(cog, interaction) -> None:
    await cog.bank_quests.callback(cog, interaction)


@pytest.mark.asyncio
async def test_quests_disabled_gate(ctx, db):
    cog = _make_cog(ctx)  # disabled
    interaction = _interaction(_member(member_id=500))
    await _quests(cog, interaction)
    args = interaction.response.send_message.await_args.args
    assert "enabled" in args[0].lower()


@pytest.mark.asyncio
async def test_quests_empty_when_none_active(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    await _quests(cog, interaction)
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "no active quests" in kwargs["embed"].description.lower()
    assert "view" not in kwargs  # nothing claimable → no select attached


@pytest.mark.asyncio
async def test_quests_listing_state_matrix(ctx, db):
    _enable(db)
    user_id = 500
    daily = _mk_quest(db, qtype="daily", title="Say hi")  # claimable
    weekly_done = _mk_quest(db, qtype="weekly", reward=40, title="Weekly grind")
    weekly_pending = _mk_quest(
        db, qtype="weekly", reward=50, signoff=1, title="Sign me off"
    )
    _mk_quest(
        db, qtype="community", reward=10, community_target=100, title="Team goal"
    )

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        # weekly_done → a paid claim this period; weekly_pending → a pending one.
        claim_quest(
            conn, settings, GUILD_ID, weekly_done, user_id,
            period=_period(db, "weekly"), booster=False,
        )
        claim_quest(
            conn, settings, GUILD_ID, weekly_pending, user_id,
            period=_period(db, "weekly"), booster=False,
        )
        conn.execute(
            "INSERT INTO econ_community_progress (quest_id, current) "
            "SELECT id, 40 FROM econ_quests WHERE title = 'Team goal'"
        )

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=user_id))
    await _quests(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    embed = kwargs["embed"]
    by_title = {f.name: f.value for f in embed.fields}
    assert any("Say hi" in n and "Ready to claim" in v for n, v in by_title.items())
    assert any("Weekly grind" in n and "Completed" in v for n, v in by_title.items())
    assert any("Sign me off" in n and "sign-off" in v.lower() for n, v in by_title.items())
    assert any("Team goal" in n and "40" in v and "100" in v for n, v in by_title.items())
    # Exactly one claimable (the daily) → select view attached.
    assert "view" in kwargs
    assert daily  # referenced


@pytest.mark.asyncio
async def test_cog_load_registers_persistent_signoff_buttons(ctx, db):
    from bot_modules.economy.quest_views import QuestApproveButton, QuestDenyButton

    bot = MagicMock()
    cog = _make_cog(ctx)
    cog.bot = bot
    await cog.cog_load()
    bot.add_dynamic_items.assert_called_once_with(QuestApproveButton, QuestDenyButton)


# ── /bank mute + notify_member honoring the pref ─────────────────────────────


async def _mute(cog, interaction) -> None:
    await cog.bank_mute.callback(cog, interaction)


@pytest.mark.asyncio
async def test_bank_mute_toggles_pref(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)

    await _mute(cog, interaction)
    with open_db(db) as conn:
        assert get_notify_muted(conn, GUILD_ID, 500) is True
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True

    # Toggling again turns notifications back on.
    interaction2 = _interaction(actor)
    await _mute(cog, interaction2)
    with open_db(db) as conn:
        assert get_notify_muted(conn, GUILD_ID, 500) is False


@pytest.mark.asyncio
async def test_bank_mute_disabled_gate(ctx, db):
    cog = _make_cog(ctx)  # disabled
    interaction = _interaction(_member(member_id=500))
    await _mute(cog, interaction)
    args = interaction.response.send_message.await_args.args
    assert "enabled" in args[0].lower()


@pytest.mark.asyncio
async def test_muted_member_not_dmd_by_notify_member(ctx, db):
    """A muted pref makes notify_member drop silently (returns True, no DM)."""
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    await _mute(cog, interaction)  # mute user 500

    dm_target = MagicMock()
    dm_target.send = AsyncMock()
    guild = MagicMock()
    guild.get_member = MagicMock(return_value=dm_target)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)

    delivered = await notify_member(bot, db, GUILD_ID, 500, content="ping")
    assert delivered is True
    dm_target.send.assert_not_called()
