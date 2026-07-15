"""Cog-level tests for /bank — wallet view, mod grant matrix, and /bank quests."""
from __future__ import annotations

import time
from pathlib import Path
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
    record_photo_card,
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
async def test_qotd_no_ping_role_posts_silently(ctx, db):
    """Default (unset) ping role keeps the original silent post."""
    _enable(db)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    await _qotd(cog, interaction, "Quiet question?")

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] is None
    assert kwargs["allowed_mentions"].roles is False


@pytest.mark.asyncio
async def test_qotd_pings_configured_role(ctx, db):
    _enable(db, qotd_ping_role_id=4242)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    await _qotd(cog, interaction, "Loud question?")

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "<@&4242>"
    # Without this the mention posts as inert text.
    assert kwargs["allowed_mentions"].roles is True


@pytest.mark.asyncio
async def test_qotd_pings_on_card_path_too(ctx, db):
    """The ping rides on content, so it must survive the card branch."""
    _enable(db, qotd_ping_role_id=4242)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    with (
        patch(
            "bot_modules.cogs.economy_cog._resolve_qotd_image",
            new=AsyncMock(return_value=b"img-bytes"),
        ),
        patch("bot_modules.cogs.economy_cog.render_quote_card", return_value=b"PNG"),
    ):
        await _qotd(cog, interaction, "Card question?")

    kwargs = channel.send.await_args.kwargs
    assert "file" in kwargs
    assert kwargs["content"] == "<@&4242>"
    assert kwargs["allowed_mentions"].roles is True


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
    trigger_words="",
    trigger_channel_id=None,
    trigger_kind="",
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
            trigger_words=trigger_words,
            trigger_channel_id=trigger_channel_id,
            trigger_kind=trigger_kind,
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
async def test_cog_load_registers_persistent_buttons(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton
    from bot_modules.economy.quest_views import QuestApproveButton, QuestDenyButton

    bot = MagicMock()
    cog = _make_cog(ctx)
    cog.bot = bot
    await cog.cog_load()
    bot.add_dynamic_items.assert_called_once_with(
        QuestApproveButton, QuestDenyButton, ShopRentButton
    )


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


# ── Stage 3: transfers / shop / role studio / gift / rentals ─────────────────

import contextlib  # noqa: E402

from bot_modules.services.voice_master_service import add_name_blocklist  # noqa: E402


def _credit(db, user_id, amount) -> None:
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, user_id, amount, "grant", actor_id=1)


def _settings(db):
    with open_db(db) as conn:
        return load_econ_settings(conn, GUILD_ID)


def _add_rental(db, perk, *, user_id=500, beneficiary_id=None, state="active") -> None:
    beneficiary_id = user_id if beneficiary_id is None else beneficiary_id
    now = time.time()
    with open_db(db) as conn:
        conn.execute(
            """
            INSERT INTO econ_rentals
                (guild_id, user_id, perk, state, price, started_at, next_bill_at,
                 cancel_at_period_end, suspended, beneficiary_id, created_at)
            VALUES (?, ?, ?, ?, 50, ?, ?, 0, 0, ?, ?)
            """,
            (GUILD_ID, user_id, perk, state, now, now + 604800, beneficiary_id, now),
        )


def _live_rentals(db) -> list:
    with open_db(db) as conn:
        return conn.execute(
            "SELECT * FROM econ_rentals WHERE state IN ('active', 'grace') "
            "ORDER BY id"
        ).fetchall()


def _guild_roles(roles=(), emojis=()) -> MagicMock:
    g = MagicMock()
    g.id = GUILD_ID
    g.roles = list(roles)
    g.emojis = list(emojis)
    return g


def _role_interaction(actor, roles=(), emojis=()) -> MagicMock:
    inter = _interaction(actor)
    inter.guild = _guild_roles(roles, emojis)
    return inter


@contextlib.contextmanager
def _patch_projection():
    """Isolate command logic from the real Discord projector / DM path."""
    with (
        patch(
            "bot_modules.cogs.economy_cog.apply_role_perks",
            new=AsyncMock(return_value=True),
        ) as apply_mock,
        patch(
            "bot_modules.cogs.economy_cog.revoke_role_perks", new=AsyncMock()
        ) as revoke_mock,
        patch(
            "bot_modules.cogs.economy_cog.notify_member",
            new=AsyncMock(return_value=True),
        ) as notify_mock,
    ):
        yield apply_mock, revoke_mock, notify_mock


# ── /bank pay ────────────────────────────────────────────────────────────────


async def _pay(cog, interaction, member, amount) -> None:
    await cog.bank_pay.callback(cog, interaction, member, amount)


@pytest.mark.asyncio
async def test_pay_immediate_under_threshold(ctx, db):
    _enable(db)
    _credit(db, 500, 200)
    cog = _make_cog(ctx)
    sender = _member(member_id=500, name="Alice")
    recipient = _member(member_id=900, name="Bob")
    interaction = _interaction(sender)

    with _patch_projection() as (_apply, _revoke, notify):
        await _pay(cog, interaction, recipient, 50)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 150
        assert get_balance(conn, GUILD_ID, 900) == 50
    notify.assert_awaited_once()
    assert notify.await_args is not None
    assert "50" in notify.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_pay_over_threshold_requires_confirm(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500)
    recipient = _member(member_id=900)
    interaction = _interaction(sender)

    with _patch_projection():
        await _pay(cog, interaction, recipient, 200)

    kwargs = interaction.response.send_message.await_args.kwargs
    from bot_modules.cogs.economy_cog import _PayConfirmView

    assert isinstance(kwargs["view"], _PayConfirmView)
    # No transfer happened yet — the gate holds.
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500
        assert get_balance(conn, GUILD_ID, 900) == 0


@pytest.mark.asyncio
async def test_pay_exactly_100_transfers_without_confirm(ctx, db):
    """Spec: confirm triggers *over* 100 — 100 itself sends straight through."""
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _pay(cog, interaction, _member(member_id=900), 100)
    assert "view" not in interaction.response.send_message.await_args.kwargs
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 100


@pytest.mark.asyncio
async def test_pay_confirm_button_executes_transfer(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500)
    recipient = _member(member_id=900)
    interaction = _interaction(sender)

    with _patch_projection():
        await _pay(cog, interaction, recipient, 200)
        view = interaction.response.send_message.await_args.kwargs["view"]
        confirm_inter = _interaction(sender)
        await view.children[0].callback(confirm_inter)  # Confirm button

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 300
        assert get_balance(conn, GUILD_ID, 900) == 200
    confirm_inter.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_pay_cancel_button_aborts(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500)
    recipient = _member(member_id=900)
    interaction = _interaction(sender)

    with _patch_projection():
        await _pay(cog, interaction, recipient, 200)
        view = interaction.response.send_message.await_args.kwargs["view"]
        cancel_inter = _interaction(sender)
        await view.children[1].callback(cancel_inter)  # Cancel button

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500
    assert "cancel" in cancel_inter.response.edit_message.await_args.kwargs["content"].lower()


@pytest.mark.asyncio
async def test_pay_transfers_disabled(ctx, db):
    _enable(db, transfers_enabled=False)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection():
        await _pay(cog, interaction, _member(member_id=900), 50)

    args = interaction.response.send_message.await_args.args
    assert "off" in args[0].lower()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500


@pytest.mark.asyncio
async def test_pay_insufficient(ctx, db):
    _enable(db)
    _credit(db, 500, 10)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection():
        await _pay(cog, interaction, _member(member_id=900), 50)

    args = interaction.response.send_message.await_args.args
    assert "enough" in args[0].lower()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 10
        assert get_balance(conn, GUILD_ID, 900) == 0


@pytest.mark.asyncio
async def test_pay_rejects_self(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500)
    interaction = _interaction(sender)
    with _patch_projection():
        await _pay(cog, interaction, sender, 50)
    assert "yourself" in interaction.response.send_message.await_args.args[0].lower()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500


@pytest.mark.asyncio
async def test_pay_rejects_bot(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500)
    interaction = _interaction(sender)
    with _patch_projection():
        await _pay(cog, interaction, _member(member_id=901, is_bot=True), 50)
    assert "bot" in interaction.response.send_message.await_args.args[0].lower()


# ── /bank shop ───────────────────────────────────────────────────────────────


async def _shop(cog, interaction) -> None:
    await cog.bank_shop.callback(cog, interaction)


@pytest.mark.asyncio
async def test_shop_lists_perks_and_gates_features(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    async def _gate(bot, guild_id, perk):
        return perk not in ("role_gradient", "role_icon")

    with patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(side_effect=_gate)):
        await _shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    from bot_modules.cogs.economy_cog import _ShopView

    view = kwargs["view"]
    assert isinstance(view, _ShopView)
    # Gradient + icon buttons disabled; colour + name enabled.
    buttons = [b for b in view.children if isinstance(b, discord.ui.Button)]
    disabled = {
        str(b.custom_id).split(":")[1] for b in buttons if b.disabled
    }
    assert disabled == {"role_gradient", "role_icon"}
    blob = " ".join(f.value for f in kwargs["embed"].fields)
    assert "requires a server feature" in blob


@pytest.mark.parametrize(
    "perk", ["role_color", "role_name", "role_gradient", "role_icon"]
)
@pytest.mark.asyncio
async def test_shop_rent_success_each_perk(ctx, db, perk):
    _enable(db)
    _credit(db, 500, 500)
    settings = _settings(db)
    price = int(getattr(settings, f"price_{perk}"))
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection() as (apply_mock, _r, _n):
        await cog.do_rent(interaction, settings, _guild_roles(), perk)

    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["perk"] == perk
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500 - price  # upfront week


@pytest.mark.asyncio
async def test_shop_shows_customise_for_rented_perks(ctx, db):
    """Rented rows swap their Rent button for a customise (modal) button."""
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)):
        await _shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    buttons = [b for b in kwargs["view"].children if isinstance(b, discord.ui.Button)]
    ids = {str(b.custom_id) for b in buttons}
    assert "econ_shop_cfg:role_color" in ids
    assert "econ_shop_rent:role_color" not in ids
    # The other perks still offer Rent.
    assert "econ_shop_rent:role_name" in ids
    blob = " ".join(f.value for f in kwargs["embed"].fields)
    assert "rented" in blob


@pytest.mark.asyncio
async def test_shop_customise_button_opens_modal(ctx, db):
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)):
        await _shop(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    button = next(
        b for b in view.children
        if isinstance(b, discord.ui.Button) and b.custom_id == "econ_shop_cfg:role_color"
    )
    press = _interaction(_member(member_id=500))
    await button.callback(press)

    from bot_modules.cogs.economy_cog import _RoleColorModal

    modal = press.response.send_modal.await_args.args[0]
    assert isinstance(modal, _RoleColorModal)


@pytest.mark.asyncio
async def test_shop_gift_recipient_gets_colour_customise(ctx, db):
    """A gifted colour (no own rental) adds a Set gifted colour button."""
    _enable(db)
    _add_rental(db, "gift_color", user_id=800, beneficiary_id=500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)):
        await _shop(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    ids = {str(b.custom_id) for b in view.children if isinstance(b, discord.ui.Button)}
    # Colour customise via the gift, while the role_color row still offers Rent.
    assert "econ_shop_cfg:gift_color" in ids
    assert "econ_shop_rent:role_color" in ids


@pytest.mark.asyncio
async def test_rent_confirmation_offers_customise_button(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection():
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "role_name")

    kwargs = interaction.response.send_message.await_args.kwargs
    buttons = [
        b for b in kwargs["view"].children if isinstance(b, discord.ui.Button)
    ]
    assert [str(b.custom_id) for b in buttons] == ["econ_rent_cfg:role_name"]


@pytest.mark.asyncio
async def test_name_modal_submit_sets_role_name(ctx, db):
    """The modal path lands on the same setter/validators as everything else."""
    _enable(db)
    _add_rental(db, "role_name")
    cog = _make_cog(ctx)

    from bot_modules.cogs.economy_cog import _RoleNameModal

    modal = _RoleNameModal(cog)
    modal.text._value = "Stardust"
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await modal.on_submit(interaction)
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT name FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["name"] == "Stardust"


@pytest.mark.asyncio
async def test_shop_rent_duplicate(ctx, db):
    _enable(db)
    _credit(db, 500, 200)
    cog = _make_cog(ctx)

    with _patch_projection():
        await cog.do_rent(_interaction(_member(member_id=500)), _settings(db), _guild_roles(), "role_color")
        interaction = _interaction(_member(member_id=500))
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "role_color")

    assert "already" in interaction.response.send_message.await_args.args[0].lower()
    assert len(_live_rentals(db)) == 1


@pytest.mark.asyncio
async def test_shop_rent_insufficient(ctx, db):
    _enable(db)
    _credit(db, 500, 10)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection():
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "role_color")

    assert "only have" in interaction.response.send_message.await_args.args[0].lower()
    assert _live_rentals(db) == []


# ── persistent shop panel ────────────────────────────────────────────────────


def _panel_channel(channel_id: int = 777) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.mention = f"<#{channel_id}>"
    ch.send = AsyncMock(return_value=MagicMock(id=8888))
    ch.fetch_message = AsyncMock()
    return ch


def _shop_panel_stored(db) -> tuple[int, int]:
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    return s.shop_channel_id, s.shop_message_id


@pytest.mark.asyncio
async def test_post_shop_posts_panel_and_saves_ids(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    channel = _panel_channel()
    interaction = _interaction(_member(admin=True))
    interaction.channel = channel

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.bank_post_shop.callback(cog, interaction, None)

    kwargs = channel.send.await_args.kwargs
    assert "Perk shop" in kwargs["embed"].title
    assert kwargs["view"].timeout is None  # persistent, never expires
    # children are DynamicItem wrappers, not raw Buttons
    assert {str(b.custom_id) for b in kwargs["view"].children} == {
        "econ_shop_panel:role_color",
        "econ_shop_panel:role_name",
        "econ_shop_panel:role_gradient",
        "econ_shop_panel:role_icon",
    }
    assert _shop_panel_stored(db) == (777, 8888)


@pytest.mark.asyncio
async def test_post_shop_refreshes_in_place_with_view(ctx, db):
    _enable(db, shop_channel_id=777, shop_message_id=4444)
    cog = _make_cog(ctx)
    channel = _panel_channel()
    old = MagicMock()
    old.edit = AsyncMock()
    channel.fetch_message.return_value = old
    interaction = _interaction(_member(admin=True))
    interaction.channel = channel

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.bank_post_shop.callback(cog, interaction, None)

    channel.fetch_message.assert_awaited_once_with(4444)
    assert "view" in old.edit.await_args.kwargs  # re-priced labels ride along
    channel.send.assert_not_awaited()
    assert _shop_panel_stored(db) == (777, 4444)


@pytest.mark.asyncio
async def test_post_shop_plain_member_refused(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    channel = _panel_channel()
    interaction = _interaction(_member())
    interaction.channel = channel

    await cog.bank_post_shop.callback(cog, interaction, None)

    assert "permission" in interaction.response.send_message.await_args.args[0]
    channel.send.assert_not_awaited()


def _panel_button_interaction(ctx, cog=None, *, member_id: int = 500) -> MagicMock:
    """The panel button reaches the cog via interaction.client.get_cog."""
    interaction = _interaction(_member(member_id=member_id))
    interaction.client = SimpleNamespace(ctx=ctx, get_cog=lambda name: cog)
    return interaction


@pytest.mark.asyncio
async def test_panel_button_rents_with_fresh_settings(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    with _patch_projection() as (apply_mock, _r, _n):
        await ShopRentButton("role_color").callback(interaction)

    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["perk"] == "role_color"
    apply_mock.assert_awaited_once()
    msg = interaction.response.send_message.await_args.args[0]
    assert "Rented" in msg
    assert interaction.response.send_message.await_args.kwargs["ephemeral"]
    # The panel's rent confirmation carries the customise button too.
    buttons = [
        b
        for b in interaction.response.send_message.await_args.kwargs["view"].children
        if isinstance(b, discord.ui.Button)
    ]
    assert [str(b.custom_id) for b in buttons] == ["econ_rent_cfg:role_color"]


@pytest.mark.asyncio
async def test_panel_button_respects_disabled_economy(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton

    interaction = _panel_button_interaction(ctx)  # economy never enabled

    await ShopRentButton("role_color").callback(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "isn't enabled" in msg
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_panel_button_rechecks_feature_gate(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton

    _enable(db)
    _credit(db, 500, 500)
    interaction = _panel_button_interaction(ctx)

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=False),
    ):
        await ShopRentButton("role_gradient").callback(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "server feature" in msg
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_panel_button_rejects_unknown_perk(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton

    _enable(db)
    interaction = _panel_button_interaction(ctx)

    await ShopRentButton("gift_color").callback(interaction)  # not self-rentable

    msg = interaction.response.send_message.await_args.args[0]
    assert "isn't available" in msg
    assert _live_rentals(db) == []


# ── role studio setters (shared by the shop's customise modals) ──────────────


async def _role_name(cog, interaction, text) -> None:
    await cog.set_role_name(interaction, text)


async def _role_color(cog, interaction, hex_) -> None:
    await cog.set_role_color(interaction, hex_)


async def _role_gradient(cog, interaction, h1, h2) -> None:
    await cog.set_role_gradient(interaction, h1, h2)


async def _role_icon_emoji(cog, interaction, raw) -> None:
    await cog.set_role_icon_emoji(interaction, raw)


async def _role_icon_image(cog, interaction, image) -> None:
    await cog.role_icon.callback(cog, interaction, image)


def _fake_emoji(name="party", eid=999, animated=False, data=b"emoji-bytes"):
    e = MagicMock()
    e.name = name
    e.id = eid
    e.animated = animated
    e.read = AsyncMock(return_value=data)
    return e


@pytest.mark.asyncio
async def test_role_name_needs_entitlement(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_name(cog, interaction, "Cool")
    assert "rent" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_name_blocklist_hit(ctx, db):
    _enable(db)
    _add_rental(db, "role_name")
    with open_db(db) as conn:
        add_name_blocklist(conn, GUILD_ID, "badword", 1)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_name(cog, interaction, "My BadWord name")
    assert "allowed" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_name_too_long(ctx, db):
    _enable(db)
    _add_rental(db, "role_name")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection():
        await _role_name(cog, interaction, "x" * 33)
    assert "32" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_role_name_success(ctx, db):
    _enable(db)
    _add_rental(db, "role_name")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_name(cog, interaction, "Stardust")
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT name FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["name"] == "Stardust"


@pytest.mark.asyncio
async def test_role_color_needs_entitlement(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#7B2FF7")
    assert "perk" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_color_bad_hex(ctx, db):
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection():
        await _role_color(cog, interaction, "not-a-colour")
    assert "hex" in interaction.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_role_color_delta_e_clash(ctx, db):
    _enable(db)
    _add_rental(db, "role_color")
    staff = MagicMock()
    staff.id = 77
    staff.name = "Admins"
    staff.colour = discord.Colour(0xFF0000)
    staff.permissions = discord.Permissions(administrator=True)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), roles=[staff])
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#FE0101")  # near-identical red
    args = interaction.response.send_message.await_args.args
    assert "Admins" in args[0]
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_color_gift_entitlement_allows(ctx, db):
    _enable(db)
    _add_rental(db, "gift_color", user_id=800, beneficiary_id=500)  # gifted to 500
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#00FF00")
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT color FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["color"] == 0x00FF00


@pytest.mark.asyncio
async def test_role_gradient_needs_feature(ctx, db):
    _enable(db)
    _add_rental(db, "role_gradient")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=False)),
    ):
        await _role_gradient(cog, interaction, "#111111", "#222222")
    assert "support" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_gradient_success(ctx, db):
    _enable(db)
    _add_rental(db, "role_gradient")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_gradient(cog, interaction, "#111111", "#222222")
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT color, color2 FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["color"] == 0x111111 and row["color2"] == 0x222222


@pytest.mark.asyncio
async def test_role_icon_without_feature(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=False)),
    ):
        await _role_icon_emoji(cog, interaction, ":party:")
    assert "support" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.parametrize("raw", [":party:", "party", "<:party:999>"])
@pytest.mark.asyncio
async def test_role_icon_custom_emoji_success(ctx, db, raw):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_emoji(cog, interaction, raw)
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT icon_path FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    # The emoji's image is downloaded into the managed icon store.
    assert Path(row["icon_path"]).read_bytes() == b"emoji-bytes"


@pytest.mark.parametrize("raw", ["✨", "<:evil:123>", ":nosuch:"])
@pytest.mark.asyncio
async def test_role_icon_rejects_non_server_emoji(ctx, db, raw):
    """Unicode emojis and emojis from other servers are refused."""
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_emoji(cog, interaction, raw)
    msg = interaction.response.send_message.await_args.args[0]
    assert "custom emoji" in msg
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_icon_rejects_animated_emoji(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    emoji = _fake_emoji(animated=True)
    interaction = _role_interaction(_member(member_id=500), emojis=[emoji])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_emoji(cog, interaction, ":party:")
    assert "animated" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_icon_image_upload_success(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    image = MagicMock()
    image.size = 100
    image.read = AsyncMock(return_value=b"png-bytes")
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_image(cog, interaction, image)
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT icon_path FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert Path(row["icon_path"]).read_bytes() == b"png-bytes"


@pytest.mark.asyncio
async def test_role_icon_image_too_big(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    image = MagicMock()
    image.size = 300 * 1024
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_image(cog, interaction, image)
    assert "256KB" in interaction.response.send_message.await_args.args[0]
    apply_mock.assert_not_awaited()


# ── /bank gift ───────────────────────────────────────────────────────────────


async def _gift(cog, interaction, member) -> None:
    await cog.bank_gift.callback(cog, interaction, member)


@pytest.mark.asyncio
async def test_gift_success_both_sides(ctx, db):
    _enable(db)
    _credit(db, 500, 50)
    cog = _make_cog(ctx)
    gifter = _member(member_id=500, name="Alice")
    friend = _member(member_id=900, name="Bob")
    interaction = _interaction(gifter)

    with _patch_projection() as (apply_mock, _r, notify):
        await _gift(cog, interaction, friend)

    rentals = _live_rentals(db)
    assert len(rentals) == 1
    assert rentals[0]["perk"] == "gift_color"
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900
    # Beneficiary's role is projected and DM'd; payer gets the confirmation.
    apply_mock.assert_awaited_once_with(cog.bot, ctx.db_path, GUILD_ID, 900)
    notify.assert_awaited_once()
    assert notify.await_args is not None
    assert notify.await_args.args[3] == 900  # DM sent to the beneficiary


@pytest.mark.asyncio
async def test_gift_rejects_self_and_bot(ctx, db):
    _enable(db)
    _credit(db, 500, 50)
    cog = _make_cog(ctx)
    gifter = _member(member_id=500)
    with _patch_projection():
        await _gift(cog, _interaction(gifter), gifter)
    interaction = _interaction(gifter)
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=901, is_bot=True))
    assert "bot" in interaction.response.send_message.await_args.args[0].lower()
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_gift_insufficient(ctx, db):
    _enable(db)
    _credit(db, 500, 10)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900))
    assert "only have" in interaction.response.send_message.await_args.args[0].lower()
    assert _live_rentals(db) == []


# ── /bank wallet: rentals field ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wallet_shows_active_rentals(ctx, db):
    _enable(db)
    _add_rental(db, "role_color", user_id=500)
    _add_rental(db, "gift_color", user_id=800, beneficiary_id=500)  # gift received
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _wallet(cog, interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    rentals_field = next(f for f in embed.fields if f.name == "Active rentals")
    assert "Custom role colour" in rentals_field.value
    assert "gift received" in rentals_field.value


# ── on_member_remove cleanup ─────────────────────────────────────────────────


async def _member_remove(cog, member) -> None:
    await cog.on_member_remove(member)


def _leaving_member(member_id) -> MagicMock:
    m = MagicMock()
    m.id = member_id
    m.guild = _guild_roles()
    return m


@pytest.mark.asyncio
async def test_member_remove_cancels_and_reprojects_all(ctx, db):
    _enable(db)
    # Leaver 500 rents a colour AND gifts a colour to friend 900.
    _add_rental(db, "role_color", user_id=500)
    _add_rental(db, "gift_color", user_id=500, beneficiary_id=900)
    cog = _make_cog(ctx)

    with _patch_projection() as (_a, revoke, _n):
        await _member_remove(cog, _leaving_member(500))

    # Both live rentals cancelled.
    assert _live_rentals(db) == []
    # Re-projected for the leaver (500) and the still-present friend (900).
    revoked_ids = {call.args[3] for call in revoke.await_args_list}
    assert revoked_ids == {500, 900}


@pytest.mark.asyncio
async def test_member_remove_beneficiary_leaving_cancels_gift(ctx, db):
    _enable(db)
    # Friend 900 leaves; the gift 500→900 must lapse.
    _add_rental(db, "gift_color", user_id=500, beneficiary_id=900)
    cog = _make_cog(ctx)

    with _patch_projection() as (_a, revoke, _n):
        await _member_remove(cog, _leaving_member(900))

    assert _live_rentals(db) == []
    revoked_ids = {call.args[3] for call in revoke.await_args_list}
    assert 900 in revoked_ids


@pytest.mark.asyncio
async def test_member_remove_skips_when_economy_disabled(ctx, db):
    _add_rental(db, "role_color", user_id=500)  # economy left disabled
    cog = _make_cog(ctx)
    with _patch_projection() as (_a, revoke, _n):
        await _member_remove(cog, _leaving_member(500))
    revoke.assert_not_awaited()
    assert len(_live_rentals(db)) == 1  # untouched


# ── trigger-word quests (spec §4.4) ─────────────────────────────────────────


def _trigger_message(
    *,
    author,
    content,
    channel_id: int = 111,
    parent_id: int | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.guild = FakeGuild(id=GUILD_ID)
    msg.author = author
    msg.content = content
    msg.channel = SimpleNamespace(id=channel_id, parent_id=parent_id)
    msg.add_reaction = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def _balance(db, user_id) -> int:
    with open_db(db) as conn:
        return get_balance(conn, GUILD_ID, user_id)


@pytest.mark.asyncio
async def test_trigger_message_pays_instant_quest_once_per_period(ctx, db):
    _enable(db)
    _mk_quest(db, reward=10, title="Say GM", trigger_words="gm, good morning")
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _trigger_message(author=member, content="GM everyone!")
    await cog._on_trigger_message(msg)
    assert _balance(db, 501) == 10
    msg.add_reaction.assert_awaited_once_with("✅")
    msg.reply.assert_awaited_once()

    # A repeat inside the same period stays silent and pays nothing more.
    repeat = _trigger_message(author=member, content="gm again")
    await cog._on_trigger_message(repeat)
    assert _balance(db, 501) == 10
    repeat.reply.assert_not_awaited()
    repeat.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_game_role_holder_gets_dm_not_channel_reply(ctx, db):
    """With game_role_id set, a role-holder is DMed the card (reaction stays,
    no in-channel reply); notify_member carries the embed."""
    _enable(db, game_role_id=777)
    _mk_quest(db, reward=10, title="Say GM", trigger_words="gm")
    cog = _make_cog(ctx)
    member = _member(member_id=501, role_ids=(777,))

    msg = _trigger_message(author=member, content="gm")
    with patch(
        "bot_modules.cogs.economy_cog.notify_member",
        new=AsyncMock(return_value=True),
    ) as notify:
        await cog._on_trigger_message(msg)

    assert _balance(db, 501) == 10
    msg.add_reaction.assert_awaited_once_with("✅")
    msg.reply.assert_not_awaited()
    notify.assert_awaited_once()
    assert notify.await_args.kwargs["embed"].title == "Quest complete!"


@pytest.mark.asyncio
async def test_game_role_non_member_paid_silently(ctx, db):
    """With game_role_id set, a member WITHOUT the role is paid silently —
    no reaction, no reply, no DM."""
    _enable(db, game_role_id=777)
    _mk_quest(db, reward=10, trigger_words="gm")
    cog = _make_cog(ctx)
    member = _member(member_id=501)  # no role_ids

    msg = _trigger_message(author=member, content="gm")
    with patch(
        "bot_modules.cogs.economy_cog.notify_member",
        new=AsyncMock(return_value=True),
    ) as notify:
        await cog._on_trigger_message(msg)

    assert _balance(db, 501) == 10  # still paid
    msg.add_reaction.assert_not_awaited()
    msg.reply.assert_not_awaited()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_game_role_signoff_dms_role_holder_and_posts_card(ctx, db):
    """A sign-off quest under game_role_id: the bank-channel card posts
    regardless, and a role-holder is DMed the 📝 card instead of a reply."""
    _enable(db, game_role_id=777)
    _mk_quest(db, reward=10, signoff=1, trigger_words="did it")
    cog = _make_cog(ctx)
    member = _member(member_id=501, role_ids=(777,))

    msg = _trigger_message(author=member, content="did it")
    with (
        patch(
            "bot_modules.cogs.economy_cog.post_signoff_card", new=AsyncMock()
        ) as card,
        patch(
            "bot_modules.cogs.economy_cog.notify_member",
            new=AsyncMock(return_value=True),
        ) as notify,
    ):
        await cog._on_trigger_message(msg)

    card.assert_awaited_once()  # manager approval flow survives the role gate
    msg.add_reaction.assert_awaited_once_with("📝")
    msg.reply.assert_not_awaited()
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_message_ignores_non_matches_and_bots(ctx, db):
    _enable(db)
    _mk_quest(db, reward=10, trigger_words="gm")
    cog = _make_cog(ctx)

    await cog._on_trigger_message(
        _trigger_message(author=_member(member_id=501), content="hello there")
    )
    await cog._on_trigger_message(
        _trigger_message(author=_member(member_id=502, is_bot=True), content="gm")
    )
    assert _balance(db, 501) == 0
    assert _balance(db, 502) == 0


@pytest.mark.asyncio
async def test_trigger_channel_scope(ctx, db):
    _enable(db)
    _mk_quest(db, reward=10, trigger_words="gm", trigger_channel_id=222)
    cog = _make_cog(ctx)

    wrong = _trigger_message(
        author=_member(member_id=501), content="gm", channel_id=111
    )
    await cog._on_trigger_message(wrong)
    assert _balance(db, 501) == 0
    wrong.reply.assert_not_awaited()

    right = _trigger_message(
        author=_member(member_id=501), content="gm", channel_id=222
    )
    await cog._on_trigger_message(right)
    assert _balance(db, 501) == 10

    # A thread under the scoped channel counts via parent_id.
    thread = _trigger_message(
        author=_member(member_id=502), content="gm",
        channel_id=333, parent_id=222,
    )
    await cog._on_trigger_message(thread)
    assert _balance(db, 502) == 10


@pytest.mark.asyncio
async def test_trigger_signoff_quest_files_pending_claim_and_card(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=10, signoff=1, trigger_words="did it")
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _trigger_message(author=member, content="I did it!")
    with patch(
        "bot_modules.cogs.economy_cog.post_signoff_card", new=AsyncMock()
    ) as card:
        await cog._on_trigger_message(msg)

    assert _balance(db, 501) == 0  # sign-off gates the payout
    card.assert_awaited_once()
    msg.add_reaction.assert_awaited_once_with("📝")
    with open_db(db) as conn:
        claim = conn.execute(
            "SELECT state FROM econ_quest_claims WHERE quest_id = ? AND user_id = 501",
            (qid,),
        ).fetchone()
    assert claim is not None and claim["state"] == "pending"


@pytest.mark.asyncio
async def test_trigger_message_noop_when_economy_disabled(ctx, db):
    _mk_quest(db, reward=10, trigger_words="gm")  # economy left disabled
    cog = _make_cog(ctx)
    msg = _trigger_message(author=_member(member_id=501), content="gm")
    await cog._on_trigger_message(msg)
    assert _balance(db, 501) == 0
    msg.reply.assert_not_awaited()


def test_trigger_quest_excluded_from_manual_claims(ctx, db):
    _enable(db)
    _mk_quest(db, title="Say GM", trigger_words="gm")
    cog = _make_cog(ctx)
    _settings, state = cog._load_quests_state(GUILD_ID, 501)
    assert [q["state"] for q in state] == ["trigger"]


# ── photo-reply event quest ─────────────────────────────────────────────────


def _mk_photo_card(db, *, message_id=9100, game_id="game-1", channel_id=111) -> None:
    with open_db(db) as conn:
        record_photo_card(conn, GUILD_ID, channel_id, message_id, game_id, "prompt")


def _photo_reply(
    *,
    author,
    ref_message_id: int | None = 9100,
    content_type: str | None = "image/png",
    filename: str = "pic.png",
    with_attachment: bool = True,
) -> MagicMock:
    msg = _trigger_message(author=author, content="")
    msg.reference = (
        SimpleNamespace(message_id=ref_message_id)
        if ref_message_id is not None
        else None
    )
    att = SimpleNamespace(content_type=content_type, filename=filename)
    msg.attachments = [att] if with_attachment else []
    return msg


@pytest.mark.asyncio
async def test_photo_reply_pays_once_per_card_across_cards(ctx, db):
    _enable(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_reply", reward=10, title="Snap it")
    _mk_photo_card(db, message_id=9100, game_id="game-1")
    _mk_photo_card(db, message_id=9200, game_id="game-2")
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _photo_reply(author=member, ref_message_id=9100)
    await cog._on_photo_reply(msg)
    assert _balance(db, 501) == 10
    msg.add_reaction.assert_awaited_once_with("✅")

    # Same card again: silent, nothing more (no time gate — the card is the key).
    repeat = _photo_reply(author=member, ref_message_id=9100)
    await cog._on_photo_reply(repeat)
    assert _balance(db, 501) == 10
    repeat.reply.assert_not_awaited()

    # A different card pays again.
    other = _photo_reply(author=member, ref_message_id=9200)
    await cog._on_photo_reply(other)
    assert _balance(db, 501) == 20


@pytest.mark.asyncio
async def test_photo_reply_requires_reply_and_image(ctx, db):
    _enable(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_reply", reward=10)
    _mk_photo_card(db)
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    # Not a reply at all.
    await cog._on_photo_reply(_photo_reply(author=member, ref_message_id=None))
    # A reply without any attachment.
    await cog._on_photo_reply(_photo_reply(author=member, with_attachment=False))
    # A reply with a non-image attachment.
    await cog._on_photo_reply(
        _photo_reply(author=member, content_type="video/mp4", filename="clip.mp4")
    )
    assert _balance(db, 501) == 0

    # No content type but an image filename counts (some mobile uploads).
    await cog._on_photo_reply(
        _photo_reply(author=member, content_type=None, filename="IMG_1234.JPG")
    )
    assert _balance(db, 501) == 10


@pytest.mark.asyncio
async def test_photo_reply_ignores_non_cards_and_needs_active_quest(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    # Card exists but no event quest is active.
    _mk_photo_card(db, message_id=9100)
    await cog._on_photo_reply(_photo_reply(author=member, ref_message_id=9100))
    assert _balance(db, 501) == 0

    # Quest active but the reply targets a message that isn't a card.
    _mk_quest(db, qtype="event", trigger_kind="photo_reply", reward=10)
    await cog._on_photo_reply(_photo_reply(author=member, ref_message_id=4242))
    assert _balance(db, 501) == 0


@pytest.mark.asyncio
async def test_photo_reply_noop_when_economy_disabled(ctx, db):
    _mk_quest(db, qtype="event", trigger_kind="photo_reply", reward=10)
    _mk_photo_card(db)
    cog = _make_cog(ctx)
    msg = _photo_reply(author=_member(member_id=501))
    await cog._on_photo_reply(msg)
    assert _balance(db, 501) == 0
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_reply_signoff_files_pending_claim(ctx, db):
    _enable(db)
    qid = _mk_quest(db, qtype="event", trigger_kind="photo_reply", reward=10, signoff=1)
    _mk_photo_card(db)
    cog = _make_cog(ctx)
    msg = _photo_reply(author=_member(member_id=501))
    with patch(
        "bot_modules.cogs.economy_cog.post_signoff_card", new=AsyncMock()
    ) as card:
        await cog._on_photo_reply(msg)

    assert _balance(db, 501) == 0  # sign-off gates the payout
    card.assert_awaited_once()
    msg.add_reaction.assert_awaited_once_with("📝")
    with open_db(db) as conn:
        claim = conn.execute(
            "SELECT state, period FROM econ_quest_claims "
            "WHERE quest_id = ? AND user_id = 501",
            (qid,),
        ).fetchone()
    assert claim is not None and claim["state"] == "pending"
    assert claim["period"] == "photo_reply:game-1"


def test_event_quest_shown_as_auto_not_claimable(ctx, db):
    _enable(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_reply", title="Snap it")
    cog = _make_cog(ctx)
    _settings, state = cog._load_quests_state(GUILD_ID, 501)
    assert [q["state"] for q in state] == ["photo_reply"]


# ── onboarding path DM ──────────────────────────────────────────────────────


def _joining_member(member_id=501, is_bot=False) -> MagicMock:
    m = _member(member_id=member_id, is_bot=is_bot)
    m.guild = FakeGuild(id=GUILD_ID)
    m.guild.name = "Test Guild"
    return m


@pytest.mark.asyncio
async def test_onboarding_dm_lists_path_once(ctx, db):
    _enable(db)
    with open_db(db) as conn:
        qid = create_quest(
            conn, GUILD_ID, title="Introduce yourself", description="",
            qtype="event", reward=20, signoff=0, criteria="",
            starts_at=None, ends_at=None, rotate_tag="",
            community_target=None, created_by=1,
            trigger_kind="bio_set", reward_xp=100, onboarding=1,
        )
        set_quest_active(conn, GUILD_ID, qid, True)
    cog = _make_cog(ctx)

    with patch(
        "bot_modules.cogs.economy_cog.notify_member", new=AsyncMock()
    ) as notify:
        await cog._on_join_onboarding(_joining_member(501))
        notify.assert_awaited_once()
        embed = notify.await_args.kwargs["embed"]
        assert "starter path" in embed.title
        field = embed.fields[0]
        assert field.name == "Introduce yourself"
        assert "20" in field.value and "100" in field.value  # coins + XP

        # A rejoin never re-DMs.
        await cog._on_join_onboarding(_joining_member(501))
        notify.assert_awaited_once()
        # Bots never get the path.
        await cog._on_join_onboarding(_joining_member(999, is_bot=True))
        notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboarding_dm_skipped_without_flagged_quests(ctx, db):
    _enable(db)
    _mk_quest(db, qtype="daily", title="Unflagged")
    cog = _make_cog(ctx)
    with patch(
        "bot_modules.cogs.economy_cog.notify_member", new=AsyncMock()
    ) as notify:
        await cog._on_join_onboarding(_joining_member(501))
        notify.assert_not_awaited()
    # And nothing was reserved — a quest flagged later still DMs this member.
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM econ_onboarding_dms WHERE guild_id = ?",
            (GUILD_ID,),
        ).fetchone()
    assert row["c"] == 0
