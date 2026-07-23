"""Cog-level tests for /bank — wallet view, mod grant matrix, and /bank quests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quests import quest_period
from bot_modules.services.economy_quests_service import (
    claim_quest,
    create_quest,
    set_income_source,
    set_quest_active,
)
from bot_modules.cogs.economy_cog import _NICK_FORBIDDEN, _custom_name_confirmation
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
    get_ledger,
    get_notify_muted,
    get_streak_shields,
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
    """resolve_accent_color reads the guild avatar — stub it to a fixed color."""
    with patch(
        "bot_modules.cogs.economy_cog.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        yield


def _make_cog(ctx):
    from bot_modules.cogs.economy_cog import EconomyCog

    return EconomyCog(MagicMock(), ctx)


def _enable(db, **overrides) -> None:
    # Set bonuses zeroed — one-quest boards would pay the clear-the-board
    # bonus on every claim and skew exact-balance assertions.
    values: dict[str, object] = {
        "enabled": True,
        "quest_set_bonus_daily": 0,
        "quest_set_bonus_weekly": 0,
    }
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
    assert embed.title == "💎 Vault"
    assert "30" in embed.description and "Gems" in embed.description
    assert "💎" in embed.description
    activity = embed.fields[0]
    # The feed renders the register's glyph + human label, not the raw kind.
    assert "🎁 Staff grant" in activity.value and "+30" in activity.value
    assert "· grant ·" not in activity.value  # never the bare snake_case token


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
    groups = {f.name: f.value or "" for f in embed.fields}
    # One line per quest, grouped by cadence: title cell | status | payment.
    daily_lines = groups["Daily"]
    assert "`Say hi" in daily_lines and "🔶 claim below" in daily_lines
    weekly_lines = groups["Weekly"]
    assert "`Weekly grind" in weekly_lines and "✅ done" in weekly_lines
    assert "`Sign me off" in weekly_lines and "⏳ sign-off" in weekly_lines
    community = groups["Community goals"]
    assert "`Team goal" in community and "▸ 40/100" in community
    # The descriptions/explainers moved behind the details select — the
    # list never carries them.
    assert all("Do the thing" not in v for v in groups.values())
    # View always attaches when quests exist (details select at minimum).
    assert "view" in kwargs
    assert daily  # referenced


@pytest.mark.asyncio
async def test_quests_event_field_stays_under_cap(ctx, db):
    # event ("Anytime") quests bypass the board's per-cadence sizing — one can
    # be active per trigger kind, and there are ~50 kinds, so dozens accrue. The
    # Anytime field must stay within Discord's 1024-char limit (each line ~45
    # chars, so 40 of them overrun) or the whole /bank quests command 400s
    # guild-wide. Overflow is summarised with a "+N more" tail.
    from bot_modules.economy.quests import TRIGGER_KINDS

    _enable(db)
    for i, kind in enumerate(list(TRIGGER_KINDS)[:40]):
        _mk_quest(
            db, qtype="event", trigger_kind=kind, reward=10,
            title=f"Anytime quest number {i:02d}",
        )
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _quests(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    embed = kwargs["embed"]
    anytime = next(f for f in embed.fields if f.name == "Anytime")
    assert len(anytime.value) <= 1024
    assert "more_" in anytime.value  # overflow summarised, not dropped silently


@pytest.mark.asyncio
async def test_cog_load_registers_persistent_buttons(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton
    from bot_modules.economy.bounty_views import (
        BountyAwardButton,
        BountyCancelButton,
        BountyChipInButton,
    )
    from bot_modules.economy.pin_views import PinApproveButton, PinDenyButton
    from bot_modules.economy.quest_views import QuestApproveButton, QuestDenyButton
    from bot_modules.economy.sponsor_views import (
        SponsorApproveButton,
        SponsorDenyButton,
    )

    bot = MagicMock()
    cog = _make_cog(ctx)
    cog.bot = bot
    await cog.cog_load()
    # Every persistent button must be re-registered here or its custom_id stops
    # routing after a restart, leaving dead buttons on old messages.
    bot.add_dynamic_items.assert_called_once_with(
        QuestApproveButton,
        QuestDenyButton,
        ShopRentButton,
        SponsorApproveButton,
        SponsorDenyButton,
        PinApproveButton,
        PinDenyButton,
        BountyChipInButton,
        BountyAwardButton,
        BountyCancelButton,
    )
    # The shop panel's Open Shop button is a static-custom_id view — it must
    # be re-registered too or the posted panel goes dead after a restart.
    from bot_modules.cogs.economy_cog import ShopPanelView

    added_views = [c.args[0] for c in bot.add_view.call_args_list]
    assert any(isinstance(v, ShopPanelView) for v in added_views)


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


def _add_rental(
    db, perk, *, user_id=500, beneficiary_id=None, state="active",
    catalog_icon_id=None,
) -> None:
    beneficiary_id = user_id if beneficiary_id is None else beneficiary_id
    now = time.time()
    with open_db(db) as conn:
        conn.execute(
            """
            INSERT INTO econ_rentals
                (guild_id, user_id, perk, state, price, started_at, next_bill_at,
                 cancel_at_period_end, suspended, beneficiary_id, catalog_icon_id,
                 created_at)
            VALUES (?, ?, ?, ?, 50, ?, ?, 0, 0, ?, ?, ?)
            """,
            (
                GUILD_ID, user_id, perk, state, now, now + 604800, beneficiary_id,
                catalog_icon_id, now,
            ),
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


def _build_shop_embed(*args, **kwargs):
    from bot_modules.cogs.economy_cog import _build_shop_embed as build

    return build(*args, **kwargs)


def _ShopView(*args, **kwargs):
    from bot_modules.cogs.economy_cog import _ShopView as view_cls

    return view_cls(*args, **kwargs)


def _shop_row(embed, label: str) -> str:
    """The shop-table line whose first code cell is ``label``."""
    for field in embed.fields:
        for line in field.value.splitlines():
            if line.startswith(f"`{label}") and "`" in line[1:]:
                return line
    raise AssertionError(f"no {label!r} row in {[f.name for f in embed.fields]}")


@pytest.mark.asyncio
async def test_shop_lists_perks_and_gates_features(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    async def _gate(bot, guild_id, perk):
        # Enhanced-role-colours off ⇒ both gradient and holographic gated.
        return perk not in ("role_gradient", "role_holographic", "role_icon")

    with patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(side_effect=_gate)):
        await _shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    from bot_modules.cogs.economy_cog import _ShopView

    view = kwargs["view"]
    assert isinstance(view, _ShopView)
    # Gradient + holographic + icon buttons disabled; color + name enabled.
    buttons = [b for b in view.children if isinstance(b, discord.ui.Button)]
    disabled = {
        str(b.custom_id).split(":")[1] for b in buttons if b.disabled
    }
    assert disabled == {"role_gradient", "role_holographic", "role_icon"}
    blob = " ".join(f.value for f in kwargs["embed"].fields)
    assert "needs a server feature" in blob


def test_shop_table_aligns_cells_and_tiers_by_price(db):
    """Rows are fixed-width code cells, grouped in tiers, cheapest first."""
    _enable(
        db,
        price_role_name=35,
        price_role_color=50,
        price_role_gradient=120,
        price_role_icon=400,
    )
    embed = _build_shop_embed(_settings(db), set(), None, panel=True)

    tiers = {f.name: f.value for f in embed.fields}
    assert list(tiers) == ["Essentials", "Signature", "One-shot", "For a Friend"]

    # Every row's cells share one width across the whole embed, so the columns
    # line up across tier headings rather than restarting at each one.
    rows = [
        line
        for value in tiers.values()
        for line in value.splitlines()
        if line.startswith("`")
    ]
    # Five self-perk rows — the "For a friend" tier is prose since gifting
    # generalized to every perk (no single gift price to tabulate).
    assert len(rows) == 5
    # One `label  blurb` cell per row (quest-board shape), all the same
    # width so columns align across tier headings — and narrow enough that
    # the price doesn't wrap onto its own line on a phone-width embed.
    cells = {line.split("`")[1] for line in rows}
    assert len({len(c) for c in cells}) == 1
    assert all(len(c) <= 27 for c in cells)

    # Ascending price inside each tier, and the blurb is present.
    assert tiers["Essentials"].index("**35**") < tiers["Essentials"].index("**50**")
    assert tiers["Signature"].index("**120**") < tiers["Signature"].index("**400**")
    assert "nickname + role" in _shop_row(embed, "Name")


def test_shop_table_reorders_when_prices_are_reconfigured(db):
    """The ladder follows the guild's prices, not the hardcoded tier order."""
    _enable(db, price_role_name=90, price_role_color=10)
    embed = _build_shop_embed(_settings(db), set(), None, panel=True)
    essentials = next(f.value for f in embed.fields if f.name == "Essentials")
    assert essentials.index("**10**") < essentials.index("**90**")


def test_shop_icon_row_shows_catalog_span_and_size(db):
    """A curated catalog prices per icon — show the span and how many there are.

    The flat custom price (default 75) folds into the span: the picker's
    bring-your-own entry sells at it, so the row's floor is min(catalog, flat).
    """
    _enable(db)
    embed = _build_shop_embed(
        _settings(db), set(), None, panel=True, icon_catalog=(120, 400, 40)
    )
    row = _shop_row(embed, "Icon")
    assert "**75–400**" in row
    assert "40 + your own" in row


def test_shop_icon_row_span_floor_is_catalog_when_below_flat(db):
    """A catalog cheaper than the flat custom price sets the floor itself."""
    _enable(db, price_role_icon=500)
    embed = _build_shop_embed(
        _settings(db), set(), None, panel=True, icon_catalog=(120, 400, 40)
    )
    assert "**120–500**" in _shop_row(embed, "Icon")


def test_shop_icon_row_collapses_a_single_priced_catalog(db):
    """One price across catalog AND flat reads as a price, not a degenerate span."""
    _enable(db, price_role_icon=200)
    embed = _build_shop_embed(
        _settings(db), set(), None, panel=True, icon_catalog=(200, 200, 3)
    )
    assert "**200**" in _shop_row(embed, "Icon")
    assert "–" not in _shop_row(embed, "Icon")


def test_shop_shows_balance_to_a_member_but_not_in_the_panel(db):
    """The wallet anchors the prices — but the channel panel is member-agnostic."""
    _enable(db)
    settings = _settings(db)
    mine = _build_shop_embed(settings, set(), None, balance=1240)
    assert "1,240" in mine.description

    panel = _build_shop_embed(settings, set(), None, panel=True)
    assert "1,240" not in panel.description
    assert "you have" not in panel.description


@pytest.mark.asyncio
async def test_shop_buttons_carry_no_price(ctx, db):
    """Prices live in the table only, so re-pricing can't stale a button label."""
    _enable(db, price_role_name=35)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)
    ):
        await _shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    labels = [
        str(b.label)
        for b in kwargs["view"].children
        if isinstance(b, discord.ui.Button)
    ]
    assert not any("35" in label for label in labels)
    assert "✨ Name" in labels


@pytest.mark.parametrize(
    "perk",
    ["role_color", "role_name", "role_gradient", "role_holographic", "role_icon"],
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
    # The rented row is ticked in the table.
    color_row = _shop_row(kwargs["embed"], "Color")
    assert "✅" in color_row


@pytest.mark.asyncio
async def test_shop_rented_holographic_shows_active_not_customise(ctx, db):
    """Holographic has no member styling, so its rented row is an inert chip."""
    _enable(db)
    _add_rental(db, "role_holographic")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)):
        await _shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    buttons = [b for b in kwargs["view"].children if isinstance(b, discord.ui.Button)]
    by_id = {str(b.custom_id): b for b in buttons}
    # Shown as active + disabled — no customise modal, no rent button.
    assert "econ_shop_active:role_holographic" in by_id
    assert by_id["econ_shop_active:role_holographic"].disabled is True
    assert "econ_shop_cfg:role_holographic" not in by_id
    assert "econ_shop_rent:role_holographic" not in by_id


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
async def test_shop_gift_recipient_gets_color_customise(ctx, db):
    """A gifted perk shows its customise button exactly like a self-rental."""
    _enable(db)
    _add_rental(db, "role_color", user_id=800, beneficiary_id=500)  # gift to 500
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch("bot_modules.cogs.economy_cog.feature_gate_ok", new=AsyncMock(return_value=True)):
        await _shop(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    ids = {str(b.custom_id) for b in view.children if isinstance(b, discord.ui.Button)}
    # The entitlement is beneficiary-based, so the row customises, not rents.
    assert "econ_shop_cfg:role_color" in ids
    assert "econ_shop_rent:role_color" not in ids


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
    assert "Perk Shop" in kwargs["embed"].title
    assert kwargs["view"].timeout is None  # persistent, never expires
    # One launcher button — the personal menu carries the per-perk buttons.
    assert {str(b.custom_id) for b in kwargs["view"].children} == {
        "econ_shop_open",
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
async def test_open_shop_panel_button_serves_the_personal_menu(ctx, db):
    from bot_modules.cogs.economy_cog import ShopPanelView, _ShopView

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await ShopPanelView()._open.callback(interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"]
    assert "Perk Shop" in kwargs["embed"].title
    assert isinstance(kwargs["view"], _ShopView)


@pytest.mark.asyncio
async def test_legacy_panel_rent_button_opens_the_personal_menu(ctx, db):
    # Pre-Open-Shop panels carry per-perk rent buttons; a click on any of
    # them now serves the personal menu instead of renting directly.
    from bot_modules.cogs.economy_cog import ShopRentButton, _ShopView

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await ShopRentButton("role_color").callback(interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"]
    assert isinstance(kwargs["view"], _ShopView)
    assert _live_rentals(db) == []  # opening the menu never rents


@pytest.mark.asyncio
async def test_panel_button_respects_disabled_economy(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton

    cog = _make_cog(ctx)  # economy never enabled
    interaction = _panel_button_interaction(ctx, cog)

    await ShopRentButton("role_color").callback(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "isn't enabled" in msg
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_panel_served_menu_rechecks_feature_gates(ctx, db):
    # The panel can outlive a feature being switched off — the menu it
    # serves re-reads the gate at click time and disables the gated rows.
    from bot_modules.cogs.economy_cog import ShopRentButton

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=False),
    ):
        await ShopRentButton("role_gradient").callback(interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    blob = " ".join(f.value for f in kwargs["embed"].fields)
    assert "needs a server feature" in blob
    buttons = [
        b for b in kwargs["view"].children if isinstance(b, discord.ui.Button)
    ]
    disabled = {str(b.custom_id) for b in buttons if b.disabled}
    assert "econ_shop_rent:role_gradient" in disabled
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_panel_button_without_cog_refuses(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton

    _enable(db)
    interaction = _panel_button_interaction(ctx)  # cog unloaded mid-flight

    await ShopRentButton("role_color").callback(interaction)

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
        await _role_color(cog, interaction, "not-a-color")
    assert "hex" in interaction.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_role_color_delta_e_clash(ctx, db):
    _enable(db)
    _add_rental(db, "role_color")
    staff = MagicMock()
    staff.id = 77
    staff.name = "Admins"
    staff.color = discord.Color(0xFF0000)
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
    _add_rental(db, "role_color", user_id=800, beneficiary_id=500)  # gifted to 500
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


# ── icon catalog picker: the Custom entry ───────────────────────────────────


def test_icon_catalog_select_last_slot_is_custom(db):
    """The picker always ends with the flat-price bring-your-own entry, and a
    big catalog trims to 24 icons so the total stays within Discord's
    25-option cap."""
    from bot_modules.cogs.economy_cog import _IconCatalogSelect

    _enable(db)
    icons = [{"id": i + 1, "name": f"Icon {i}", "price": 100} for i in range(30)]
    select = _IconCatalogSelect(MagicMock(), _settings(db), _guild_roles(), icons)
    assert len(select.options) == 25
    assert select.options[-1].value == "custom"
    assert "75" in select.options[-1].description  # the flat price_role_icon
    assert [o.value for o in select.options[:24]] == [str(i + 1) for i in range(24)]


@pytest.mark.asyncio
async def test_pick_custom_icon_rents_flat_when_unowned(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch(
            "bot_modules.cogs.economy_cog.feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_custom_icon(interaction, _settings(db), _guild_roles())
    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["perk"] == "role_icon"
    assert rentals[0]["catalog_icon_id"] is None
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500 - 75  # flat upfront week
    apply_mock.assert_awaited_once()
    # The confirmation points at the image-upload path.
    assert "/bank role icon" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_pick_custom_icon_switches_catalog_rental_free_and_clears_art(ctx, db):
    """Catalog → custom re-tags the paid week (no charge) and blanks the
    projected image — the catalog art belongs to the catalog price."""
    from bot_modules.services.economy_icon_catalog_service import add_catalog_icon
    from bot_modules.services.economy_rentals_service import (
        rent_perk,
        upsert_personal_role,
    )

    _enable(db)
    _credit(db, 500, 500)
    settings = _settings(db)
    with open_db(db) as conn:
        icon_id = add_catalog_icon(conn, GUILD_ID, name="Crown", price=100)
        rent_perk(
            conn, settings, GUILD_ID, 500, "role_icon", catalog_icon_id=icon_id
        )
        upsert_personal_role(conn, GUILD_ID, 500, {"icon_path": "/icons/crown.png"})
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch(
            "bot_modules.cogs.economy_cog.feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_custom_icon(interaction, settings, _guild_roles())
    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["catalog_icon_id"] is None
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 400  # only the upfront 100
        row = conn.execute(
            "SELECT icon_path FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["icon_path"] == ""
    apply_mock.assert_awaited_once()
    assert "next renewal" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_pick_custom_icon_already_custom_just_offers_customise(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.pick_custom_icon(interaction, _settings(db), _guild_roles())
    assert len(_live_rentals(db)) == 1  # no double rent, no charge
    call = interaction.response.send_message.await_args
    assert "already" in call.args[0]
    assert call.kwargs["view"] is not None


@pytest.mark.asyncio
async def test_role_icon_upload_blocked_for_catalog_renters_only(ctx, db):
    """The upload guard is per-rental, not per-guild: a curated-catalog renter
    is locked to their icon, while a flat-price custom renter uploads freely
    even though the guild stocks a catalog."""
    from bot_modules.services.economy_icon_catalog_service import add_catalog_icon

    _enable(db)
    with open_db(db) as conn:
        icon_id = add_catalog_icon(conn, GUILD_ID, name="Crown", price=100)
    _add_rental(db, "role_icon", user_id=500, catalog_icon_id=icon_id)
    _add_rental(db, "role_icon", user_id=600)
    cog = _make_cog(ctx)
    assert cog._catalog_locked(GUILD_ID, 500) is True
    assert cog._catalog_locked(GUILD_ID, 600) is False
    assert cog._catalog_locked(GUILD_ID, 700) is False  # no rental at all

    locked = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    free = _role_interaction(_member(member_id=600), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch(
            "bot_modules.cogs.economy_cog.feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _role_icon_emoji(cog, locked, ":party:")
        await _role_icon_emoji(cog, free, ":party:")
    assert "Custom" in locked.response.send_message.await_args.args[0]
    apply_mock.assert_awaited_once()  # only the custom renter's upload landed


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


async def _gift(cog, interaction, member, perk="role_color") -> None:
    from bot_modules.cogs.economy_cog import _PERK_LABELS

    choice = app_commands.Choice(name=_PERK_LABELS[perk], value=perk)
    await cog.bank_gift.callback(cog, interaction, member, choice)


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
    assert rentals[0]["perk"] == "role_color"
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900
    # Beneficiary's role is projected and DM'd; payer gets the confirmation.
    apply_mock.assert_awaited_once_with(cog.bot, ctx.db_path, GUILD_ID, 900)
    notify.assert_awaited_once()
    assert notify.await_args is not None
    assert notify.await_args.args[3] == 900  # DM sent to the beneficiary


@pytest.mark.asyncio
async def test_gift_any_perk_bills_base_price(ctx, db):
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))

    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="role_name")

    rentals = _live_rentals(db)
    assert len(rentals) == 1
    assert rentals[0]["perk"] == "role_name"
    assert rentals[0]["price"] == 35  # the base perk price, no gift surcharge
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900


@pytest.mark.asyncio
async def test_gift_feature_gated_perk_refused_when_gate_closed(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with (
        _patch_projection(),
        patch(
            "bot_modules.cogs.economy_cog.feature_gate_ok",
            new=AsyncMock(return_value=False),
        ),
    ):
        await _gift(cog, interaction, _member(member_id=900), perk="role_gradient")

    assert "server feature" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_gift_duplicate_entitlement_requires_confirm(ctx, db):
    """Gifting a perk the friend already has stops at a confirm view."""
    from bot_modules.cogs.economy_cog import _GiftConfirmView

    _enable(db)
    _credit(db, 500, 100)
    _add_rental(db, "role_color", user_id=900)  # friend self-rents it already
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900))

    # No rental opened yet — the reply is the Gift anyway? confirm gate.
    assert len(_live_rentals(db)) == 1  # just the friend's own rental
    kwargs = interaction.response.send_message.await_args.kwargs
    assert isinstance(kwargs["view"], _GiftConfirmView)
    assert "already has" in interaction.response.send_message.await_args.args[0]


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
    _add_rental(db, "role_color", user_id=800, beneficiary_id=500)  # gift received
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _wallet(cog, interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    rentals_field = next(f for f in embed.fields if f.name == "Active Rentals")
    assert "Custom Role Color" in rentals_field.value
    assert "gift received" in rentals_field.value


@pytest.mark.asyncio
async def test_wallet_active_rentals_field_stays_under_cap(ctx, db):
    # A popular member on the receiving end of many gifts can accrue a dozen+
    # live rentals; each renders ~70 chars, so the joined field would blow past
    # Discord's 1024-char cap and 400 the whole wallet embed. _fit_lines trims.
    _enable(db)
    for payer in range(800, 830):  # 30 distinct gifters -> 30 live rentals
        _add_rental(db, "role_color", user_id=payer, beneficiary_id=500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _wallet(cog, interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    rentals_field = next(f for f in embed.fields if f.name == "Active Rentals")
    assert len(rentals_field.value) <= 1024
    assert "Custom Role Color" in rentals_field.value


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
    # Leaver 500 rents a color AND gifts a color to friend 900.
    _add_rental(db, "role_color", user_id=500)
    _add_rental(db, "role_color", user_id=500, beneficiary_id=900)
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
    _add_rental(db, "role_color", user_id=500, beneficiary_id=900)
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
    msg.reply.assert_not_awaited()

    # A repeat inside the same period stays silent and pays nothing more.
    repeat = _trigger_message(author=member, content="gm again")
    await cog._on_trigger_message(repeat)
    assert _balance(db, 501) == 10
    repeat.reply.assert_not_awaited()
    repeat.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_quest_completion_is_reaction_only_regardless_of_role(ctx, db):
    """No reply, no DM either way — game_role_id no longer affects this path
    (it still gates the daily digest / raffle-winner notices elsewhere)."""
    _enable(db, game_role_id=777)
    _mk_quest(db, reward=10, title="Say GM", trigger_words="gm")
    cog = _make_cog(ctx)

    for member in (_member(member_id=501, role_ids=(777,)), _member(member_id=502)):
        msg = _trigger_message(author=member, content="gm")
        with patch(
            "bot_modules.cogs.economy_cog.notify_member",
            new=AsyncMock(return_value=True),
        ) as notify:
            await cog._on_trigger_message(msg)

        assert _balance(db, member.id) == 10
        msg.add_reaction.assert_awaited_once_with("✅")
        msg.reply.assert_not_awaited()
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_signoff_trigger_quest_posts_card_and_reacts_only(ctx, db):
    """A sign-off quest still files the claim and posts the manager card, but
    the member only gets the 📝 reaction — no reply, no DM, regardless of
    game_role_id."""
    _enable(db, game_role_id=777)
    _mk_quest(db, reward=10, signoff=1, trigger_words="did it")
    cog = _make_cog(ctx)

    for member in (_member(member_id=501, role_ids=(777,)), _member(member_id=502)):
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

        card.assert_awaited_once()
        msg.add_reaction.assert_awaited_once_with("📝")
        msg.reply.assert_not_awaited()
        notify.assert_not_awaited()


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
    _settings, state, _meta = cog._load_quests_state(GUILD_ID, 501)
    assert [q["state"] for q in state] == ["trigger"]


# ── per-member board size (configurable) ────────────────────────────────────


def test_board_size_limits_quests_shown(ctx, db):
    # Six active dailies, board sized to 1 → the member sees exactly one.
    _enable(db, quest_board_daily=1)
    for i in range(6):
        _mk_quest(db, title=f"Daily {i}")
    cog = _make_cog(ctx)
    _settings, state, _meta = cog._load_quests_state(GUILD_ID, 501)
    assert len(state) == 1


def test_board_size_zero_shows_no_quests(ctx, db):
    # 0 = cadence off. Guards the inverse regression: gating the board filter
    # on "size > 0" would skip filtering entirely and show the whole pool.
    _enable(db, quest_board_daily=0)
    for i in range(6):
        _mk_quest(db, title=f"Daily {i}")
    cog = _make_cog(ctx)
    _settings, state, _meta = cog._load_quests_state(GUILD_ID, 501)
    assert state == []


@pytest.mark.asyncio
async def test_board_size_zero_blocks_trigger_word_claim(ctx, db):
    # The third board gate (the trigger-word on_message path). With the
    # cadence off, saying the phrase must pay nothing rather than fall
    # through to an unfiltered claim.
    _enable(db, quest_board_daily=0)
    _mk_quest(db, reward=10, trigger_words="gm")
    cog = _make_cog(ctx)
    msg = _trigger_message(author=_member(member_id=501), content="gm")
    await cog._on_trigger_message(msg)
    assert _balance(db, 501) == 0
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_word_still_pays_with_board_size_one(ctx, db):
    # Control for the test above: the phrase pays when the cadence is on, so
    # the 0 case is proving the gate and not a broken fixture.
    _enable(db, quest_board_daily=1)
    _mk_quest(db, reward=10, trigger_words="gm")
    cog = _make_cog(ctx)
    msg = _trigger_message(author=_member(member_id=501), content="gm")
    await cog._on_trigger_message(msg)
    assert _balance(db, 501) == 10


def test_board_size_zero_round_trips_through_settings(db):
    # The dial is only usable if a stored "0" loads back as 0 rather than
    # falling through to the default board size.
    _enable(db, quest_board_daily=0, quest_board_weekly=3)
    with open_db(db) as conn:
        loaded = load_econ_settings(conn, GUILD_ID)
    assert loaded.quest_board_daily == 0
    assert loaded.quest_board_weekly == 3
    assert loaded.quest_board_monthly == 2  # untouched → default


# ── photo-post event quest ──────────────────────────────────────────────────

PHOTO_CHANNEL_ID = 111


def _set_photo_config(db, *, channel_id=PHOTO_CHANNEL_ID) -> None:
    opts: dict[str, object] = {"channel_id": str(channel_id) if channel_id else ""}
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled, options)"
            " VALUES (?, 'photo', 1, ?)"
            " ON CONFLICT(guild_id, game_type) DO UPDATE SET options = excluded.options",
            (GUILD_ID, json.dumps(opts)),
        )
        conn.commit()


def _set_photo_schedule(db, *, channel_id=PHOTO_CHANNEL_ID, status="active") -> None:
    """Insert a minimal photo schedule row (games_scheduled), no config row.

    Mirrors the live desync where a schedule was created but the Photo
    Challenge Setup panel (which owns the games_game_config channel) was
    never saved, so the award listener must recover the channel here.
    """
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO games_scheduled"
            " (guild_id, channel_id, game_type, created_by, created_at,"
            "  time_of_day, recurrence, status)"
            " VALUES (?, ?, 'photo', 1, 0, 540, 'daily', ?)",
            (GUILD_ID, channel_id, status),
        )
        conn.commit()


def _today_period(db) -> str:
    with open_db(db) as conn:
        offset = get_tz_offset_hours(conn, GUILD_ID)
    return f"photo_post:{local_day_for(time.time(), offset)}"


def _photo_msg(
    *,
    author,
    message_id: int = 9100,
    channel_id: int = PHOTO_CHANNEL_ID,
    parent_id: int | None = None,
    content_type: str | None = "image/png",
    filename: str = "pic.png",
    with_attachment: bool = True,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.guild = FakeGuild(id=GUILD_ID)
    msg.author = author
    msg.channel = SimpleNamespace(id=channel_id, parent_id=parent_id)
    att = SimpleNamespace(content_type=content_type, filename=filename)
    msg.attachments = [att] if with_attachment else []
    msg.add_reaction = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def _disable_photo_source(db) -> None:
    with open_db(db) as conn:
        set_income_source(conn, GUILD_ID, "photo_post", False)
        conn.commit()


@pytest.mark.asyncio
async def test_photo_post_participation_pays_without_quest(ctx, db):
    # The flat participation award pays on the post itself — no quest needed.
    _enable(db)  # reward_photo_post defaults to 5
    _set_photo_config(db)
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _photo_msg(author=member)
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 5
    msg.add_reaction.assert_awaited_once_with("✅")

    # A second photo the same day pays nothing more — once per local day.
    await cog._on_photo_post(_photo_msg(author=member, message_id=9200))
    assert _balance(db, 501) == 5


@pytest.mark.asyncio
async def test_photo_post_pays_in_thread_of_photo_channel(ctx, db):
    # A photo posted in a THREAD of the Photo Challenge channel earns too:
    # the listener matches on parent_id, not just the exact channel id (mirrors
    # the trigger-quest / games siblings). Previously such a post paid nothing.
    _enable(db)  # reward_photo_post defaults to 5
    _set_photo_config(db)  # scoped channel is PHOTO_CHANNEL_ID
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _photo_msg(
        author=member, channel_id=999_888, parent_id=PHOTO_CHANNEL_ID
    )
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 5
    msg.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_photo_post_ignores_unrelated_channel(ctx, db):
    # A photo in a channel that is neither the scoped channel nor a thread of it
    # pays nothing — the parent_id widening must not swallow the whole guild.
    _enable(db)
    _set_photo_config(db)
    cog = _make_cog(ctx)

    msg = _photo_msg(
        author=_member(member_id=501), channel_id=999_888, parent_id=555_444
    )
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 0


@pytest.mark.asyncio
async def test_photo_post_pays_via_schedule_channel_without_config(ctx, db):
    # Regression: a photo schedule exists but the Setup panel was never saved,
    # so games_game_config has no 'photo' row. The award listener must recover
    # the channel from the active schedule and still pay — no more silent misses.
    _enable(db)  # participation 5, no config row written
    _set_photo_schedule(db)  # active schedule points at PHOTO_CHANNEL_ID
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _photo_msg(author=member)
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 5
    msg.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_photo_post_ignores_done_schedule_channel(ctx, db):
    # A finished (status='done') schedule is not a live channel — the fallback
    # only recovers from an *active* schedule, so a post here pays nothing.
    _enable(db)  # participation 5, no config row
    _set_photo_schedule(db, status="done")
    cog = _make_cog(ctx)

    await cog._on_photo_post(_photo_msg(author=_member(member_id=501)))
    assert _balance(db, 501) == 0


@pytest.mark.asyncio
async def test_photo_post_config_channel_wins_over_schedule(ctx, db):
    # When the config row carries a channel, it is authoritative even if a
    # schedule points elsewhere — the fallback only fires on an empty config.
    _enable(db)  # participation 5
    _set_photo_config(db, channel_id=PHOTO_CHANNEL_ID)
    _set_photo_schedule(db, channel_id=222)  # different channel
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    # A post in the schedule's channel is ignored (config channel is the gate).
    await cog._on_photo_post(_photo_msg(author=member, channel_id=222))
    assert _balance(db, 501) == 0
    # A post in the configured channel pays.
    await cog._on_photo_post(_photo_msg(author=member, message_id=9300))
    assert _balance(db, 501) == 5


@pytest.mark.asyncio
async def test_photo_post_quest_stacks_on_participation(ctx, db):
    # Participation (5) + an active photo_post quest (10) both pay = 15.
    _enable(db)  # reward_photo_post 5
    _set_photo_config(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10, title="Snap it")
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _photo_msg(author=member)
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 15
    # The quest outcome carries the ✅ (participation doesn't add a second one).
    msg.add_reaction.assert_awaited_once_with("✅")

    # A second photo the same day pays nothing more — both sides cap per day.
    await cog._on_photo_post(_photo_msg(author=member, message_id=9200))
    assert _balance(db, 501) == 15


@pytest.mark.asyncio
async def test_photo_post_no_payout_when_source_disabled(ctx, db):
    # The photo_post income-source toggle gates both payouts.
    _enable(db)  # participation 5
    _set_photo_config(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10)
    _disable_photo_source(db)
    cog = _make_cog(ctx)
    msg = _photo_msg(author=_member(member_id=501))
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 0
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_post_gated_by_channel_and_image(ctx, db):
    # Participation off so this isolates the channel/image/quest gating.
    _enable(db, reward_photo_post=0)
    _set_photo_config(db)
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    # No active photo_post quest and no participation → the gate short-circuits.
    await cog._on_photo_post(_photo_msg(author=member))
    assert _balance(db, 501) == 0

    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10)
    cog = _make_cog(ctx)  # fresh channel cache

    # A post in some other channel is ignored (cheap channel gate).
    await cog._on_photo_post(
        _photo_msg(author=member, channel_id=222, message_id=9400)
    )
    assert _balance(db, 501) == 0

    # A non-image post in the channel is ignored.
    await cog._on_photo_post(
        _photo_msg(author=member, with_attachment=False, message_id=9401)
    )
    assert _balance(db, 501) == 0

    # A real image post in the channel pays the quest (participation off).
    await cog._on_photo_post(_photo_msg(author=member, message_id=9402))
    assert _balance(db, 501) == 10


@pytest.mark.asyncio
async def test_photo_post_ignores_bot_author(ctx, db):
    _enable(db)  # participation on
    _set_photo_config(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10)
    cog = _make_cog(ctx)

    # A bot posting an image never earns (the author.bot guard) — neither the
    # participation award nor the quest.
    bot_author = _member(member_id=777, is_bot=True)
    await cog._on_photo_post(_photo_msg(author=bot_author))
    assert _balance(db, 777) == 0


@pytest.mark.asyncio
async def test_photo_post_noop_when_economy_disabled(ctx, db):
    _set_photo_config(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10)
    cog = _make_cog(ctx)  # economy left disabled
    msg = _photo_msg(author=_member(member_id=501))
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 0
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_post_signoff_files_pending_claim(ctx, db):
    # Participation off so the balance isolates the sign-off gating.
    _enable(db, reward_photo_post=0)
    qid = _mk_quest(
        db, qtype="event", trigger_kind="photo_post", reward=10, signoff=1
    )
    _set_photo_config(db)
    cog = _make_cog(ctx)
    msg = _photo_msg(author=_member(member_id=501))
    with patch(
        "bot_modules.cogs.economy_cog.post_signoff_card", new=AsyncMock()
    ) as card:
        await cog._on_photo_post(msg)

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
    assert claim["period"] == _today_period(db)


def test_event_quest_shown_as_auto_not_claimable(ctx, db):
    _enable(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", title="Snap it")
    cog = _make_cog(ctx)
    _settings, state, _meta = cog._load_quests_state(GUILD_ID, 501)
    assert [q["state"] for q in state] == ["photo_post"]


# ── pay memo ─────────────────────────────────────────────────────────


def test_clean_memo_collapses_whitespace_and_caps_length():
    from bot_modules.cogs.economy_cog import _MAX_MEMO_LEN, _clean_memo

    assert _clean_memo("  rent   money  ") == "rent money"
    # Newlines would break the one-line wallet/ledger renders.
    assert _clean_memo("rent\nmoney") == "rent money"
    assert _clean_memo(None) is None
    assert _clean_memo("   ") is None
    assert len(_clean_memo("x" * 500)) == _MAX_MEMO_LEN


def test_memo_of_tolerates_missing_and_malformed_meta():
    from bot_modules.cogs.economy_cog import _memo_of

    assert _memo_of('{"to": 1, "memo": "hi"}') == "hi"
    assert _memo_of('{"to": 1}') is None
    assert _memo_of(None) is None
    assert _memo_of("") is None
    assert _memo_of("not json") is None
    # A non-string memo must not crash the render.
    assert _memo_of('{"memo": 5}') is None


def test_fit_lines_keeps_newest_rows_under_the_field_cap():
    from bot_modules.cogs.economy_cog import _fit_lines

    short = ["a", "b", "c"]
    assert _fit_lines(short) == "a\nb\nc"
    # Ten max-length memo rows must not overrun the 1024-char embed field.
    fat = [("x" * 200) for _ in range(10)]
    out = _fit_lines(fat)
    assert len(out) <= 1024
    assert out.startswith("x")


async def _pay(cog, interaction, member, amount, memo=None) -> None:
    await cog.bank_pay.callback(cog, interaction, member, amount, memo)


@pytest.mark.asyncio
async def test_pay_memo_reaches_ledger_embed_and_dm(ctx, db):
    _enable(db, transfers_enabled=True)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 50, "grant")

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Payer"))
    recipient = _member(member_id=600, name="Payee")

    with patch("bot_modules.cogs.economy_cog.notify_member", new=AsyncMock()) as notify:
        await _pay(cog, interaction, recipient, 20, "  rent   money ")

    # Sender's confirmation embed carries the normalised memo.
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert (embed.title or "").endswith("Payment Sent")
    assert "rent money" in embed.description

    # Recipient's DM carries it too.
    assert "rent money" in notify.await_args.kwargs["content"]

    # And both ledger sides persist it.
    with open_db(db) as conn:
        out = get_ledger(conn, GUILD_ID, 500, limit=1)[0]
        inc = get_ledger(conn, GUILD_ID, 600, limit=1)[0]
    import json

    assert json.loads(out["meta"])["memo"] == "rent money"
    assert json.loads(inc["meta"])["memo"] == "rent money"


@pytest.mark.asyncio
async def test_pay_without_memo_is_unchanged(ctx, db):
    _enable(db, transfers_enabled=True)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 50, "grant")

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with patch("bot_modules.cogs.economy_cog.notify_member", new=AsyncMock()) as notify:
        await _pay(cog, interaction, _member(member_id=600), 20)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert (embed.title or "").endswith("Payment Sent")
    # A memo would appear as its own trailing paragraph; the base line has none.
    assert "\n\n" not in embed.description
    assert '"' not in notify.await_args.kwargs["content"]
    with open_db(db) as conn:
        assert "memo" not in get_ledger(conn, GUILD_ID, 500, limit=1)[0]["meta"]


@pytest.mark.asyncio
async def test_pay_memo_cannot_ping_via_the_dm_path(ctx, db):
    """The DM/bank-channel fallback sends raw content — @everyone must not ping."""
    _enable(db, transfers_enabled=True)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 50, "grant")

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with patch("bot_modules.cogs.economy_cog.notify_member", new=AsyncMock()) as notify:
        await _pay(cog, interaction, _member(member_id=600), 20, "@everyone pay up")

    assert "@everyone" not in notify.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_pay_memo_survives_the_large_amount_confirm_gate(ctx, db):
    _enable(db, transfers_enabled=True)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 5000, "grant")

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    recipient = _member(member_id=600)
    await _pay(cog, interaction, recipient, 500, "big one")

    # Over the threshold we get a confirm view, not a transfer.
    kwargs = interaction.response.send_message.await_args.kwargs
    assert (kwargs["embed"].title or "").endswith("Confirm Payment")
    assert "big one" in kwargs["embed"].description
    view = kwargs["view"]
    assert view.memo == "big one"

    # Confirming carries the memo through to the ledger.
    confirm_button = next(c for c in view.children if c.label == "Confirm")
    confirm_inter = _interaction(_member(member_id=500))
    with patch("bot_modules.cogs.economy_cog.notify_member", new=AsyncMock()):
        await confirm_button.callback(confirm_inter)
    with open_db(db) as conn:
        import json

        assert json.loads(get_ledger(conn, GUILD_ID, 500, limit=1)[0]["meta"])[
            "memo"
        ] == "big one"


# ── streak shield in the shop (sinks round 3, stage 2) ───────────────────────


def test_shop_embed_shield_row_and_held_marker(db):
    _enable(db)
    embed = _build_shop_embed(_settings(db), set(), None, panel=True)
    row = next(f for f in embed.fields if f.name == "One-shot")
    assert "Streak shield" in row.value
    assert "held" not in row.value
    held = _build_shop_embed(_settings(db), set(), None, shields_held=1)
    assert "held" in next(f for f in held.fields if f.name == "One-shot").value


def test_shop_embed_hides_shield_at_price_zero(db):
    _enable(db, price_streak_shield=0)
    embed = _build_shop_embed(_settings(db), set(), None, panel=True)
    assert not any(f.name == "One-shot" for f in embed.fields)


@pytest.mark.asyncio
async def test_shop_view_shield_button_disabled_while_held(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    view = _ShopView(cog, _settings(db), _guild_roles(), 500, set(), set())
    button = next(
        b for b in view.children
        if isinstance(b, discord.ui.Button) and b.custom_id == "econ_shop_shield"
    )
    assert button.disabled is False
    held = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(), shields_held=1
    )
    button = next(
        b for b in held.children
        if isinstance(b, discord.ui.Button) and b.custom_id == "econ_shop_shield"
    )
    assert button.disabled is True


@pytest.mark.asyncio
async def test_buy_shield_debits_and_confirms(ctx, db):
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_buy_shield(interaction, _settings(db), _guild_roles())

    assert "ready" in interaction.response.send_message.await_args.args[0]
    with open_db(db) as conn:
        assert get_streak_shields(conn, GUILD_ID, 500) == 1
        assert get_balance(conn, GUILD_ID, 500) == 100 - 30


@pytest.mark.asyncio
async def test_buy_shield_already_holding_message(ctx, db):
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    await cog.do_buy_shield(
        _interaction(_member(member_id=500)), _settings(db), _guild_roles()
    )
    interaction = _interaction(_member(member_id=500))
    await cog.do_buy_shield(interaction, _settings(db), _guild_roles())
    assert "already holding" in interaction.response.send_message.await_args.args[0]
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 100 - 30  # charged once


# ── /bank shop: cancel & refund ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_shop_view_shows_refund_button_only_when_something_refundable(ctx, db):
    cog = _make_cog(ctx)
    bare = _ShopView(cog, _settings(db), _guild_roles(), 500, set(), set())
    assert not any(
        b.custom_id == "econ_shop_refund"
        for b in bare.children
        if isinstance(b, discord.ui.Button)
    )
    with_rental = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(),
        refundable=[
            {
                "id": 1, "perk": "role_color", "state": "active",
                "price": 50, "next_bill_at": time.time() + 604800,
            }
        ],
    )
    assert any(
        b.custom_id == "econ_shop_refund"
        for b in with_rental.children
        if isinstance(b, discord.ui.Button)
    )
    with_shield = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(), shield_price=30,
    )
    assert any(
        b.custom_id == "econ_shop_refund"
        for b in with_shield.children
        if isinstance(b, discord.ui.Button)
    )


@pytest.mark.asyncio
async def test_refund_picker_view_scoped_to_its_own_member(ctx, db):
    # _RefundPickerView shares its ownership guard with _ShopView/
    # _IconCatalogView via _MemberScopedView — confirm the shared base still
    # rejects a different member's click.
    from bot_modules.cogs.economy_cog import _RefundPickerView

    cog = _make_cog(ctx)
    view = _RefundPickerView(cog, _settings(db), _guild_roles(), 500, [], 30)
    stranger = _interaction(_member(member_id=999))
    assert await view.interaction_check(stranger) is False
    assert "Open your own shop" in stranger.response.send_message.await_args.args[0]
    owner = _interaction(_member(member_id=500))
    assert await view.interaction_check(owner) is True


@pytest.mark.asyncio
async def test_refund_picker_previews_prorated_amount(ctx, db):
    _enable(db)
    _credit(db, 500, 200)
    _add_rental(db, "role_color", user_id=500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    refundable, _shields_held, shield_price = cog._refundables(
        GUILD_ID, 500, _settings(db)
    )

    await cog.open_refund_picker(
        interaction, _settings(db), _guild_roles(), refundable, shield_price
    )
    view = interaction.response.send_message.await_args.kwargs["view"]
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))
    assert len(select.options) == 1
    assert select.options[0].value.startswith("rental:")
    assert "back" in select.options[0].description


@pytest.mark.asyncio
async def test_refund_confirm_credits_prorated_amount_and_ends_rental_now(ctx, db):
    _enable(db)
    _credit(db, 500, 200)
    _add_rental(db, "role_color", user_id=500)
    with open_db(db) as conn:
        rid = conn.execute(
            "SELECT id FROM econ_rentals WHERE user_id = 500"
        ).fetchone()["id"]
        # Half the paid week already elapsed -> half the price back.
        conn.execute(
            "UPDATE econ_rentals SET next_bill_at = ? WHERE id = ?",
            (time.time() + 302400, rid),
        )
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.revoke_perk_effect", new=AsyncMock()
    ) as revoke_mock:
        await cog.finalize_refund(
            interaction, _settings(db), _guild_roles(), f"rental:{rid}"
        )
    revoke_mock.assert_awaited_once()
    text = interaction.response.edit_message.await_args.kwargs["content"]
    assert "credited back" in text
    with open_db(db) as conn:
        # _add_rental inserts the row directly (no debit), so the refund is
        # pure credit on top of the starting 200 — ~half the 50 price back,
        # floor rounding tolerating a little clock jitter.
        balance = get_balance(conn, GUILD_ID, 500)
        assert 200 + 24 <= balance <= 200 + 25
        row = conn.execute(
            "SELECT state FROM econ_rentals WHERE id = ?", (rid,)
        ).fetchone()
        assert row["state"] == "cancelled"  # ends now, not at period end


@pytest.mark.asyncio
async def test_refund_confirm_survives_a_failed_perk_revoke(ctx, db):
    # The refund (money + rental state) already committed by the time
    # revoke_perk_effect runs — a Discord-side failure there must not blow
    # up the interaction and strand the member with no confirmation.
    _enable(db)
    _credit(db, 500, 200)
    _add_rental(db, "role_color", user_id=500)
    with open_db(db) as conn:
        rid = conn.execute(
            "SELECT id FROM econ_rentals WHERE user_id = 500"
        ).fetchone()["id"]
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.revoke_perk_effect",
        new=AsyncMock(side_effect=RuntimeError("discord blew up")),
    ):
        await cog.finalize_refund(
            interaction, _settings(db), _guild_roles(), f"rental:{rid}"
        )
    text = interaction.response.edit_message.await_args.kwargs["content"]
    assert "credited back" in text  # still shows success, doesn't raise
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT state FROM econ_rentals WHERE id = ?", (rid,)
        ).fetchone()
        assert row["state"] == "cancelled"  # the refund itself is unaffected


@pytest.mark.asyncio
async def test_refund_confirm_shield_credits_full_price(ctx, db):
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    await cog.do_buy_shield(
        _interaction(_member(member_id=500)), _settings(db), _guild_roles()
    )

    interaction = _interaction(_member(member_id=500))
    await cog.finalize_refund(interaction, _settings(db), _guild_roles(), "shield")

    text = interaction.response.edit_message.await_args.kwargs["content"]
    assert "30" in text  # the shield's default price
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 100
        assert get_streak_shields(conn, GUILD_ID, 500) == 0


@pytest.mark.asyncio
async def test_refund_confirm_rejects_a_stale_target(ctx, db):
    _enable(db)
    _credit(db, 500, 200)
    _add_rental(db, "role_color", user_id=500, state="lapsed")
    with open_db(db) as conn:
        rid = conn.execute(
            "SELECT id FROM econ_rentals WHERE user_id = 500"
        ).fetchone()["id"]
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.finalize_refund(
        interaction, _settings(db), _guild_roles(), f"rental:{rid}"
    )
    text = interaction.response.edit_message.await_args.kwargs["content"]
    assert "❌" in text
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 200  # untouched


# ── voice-style lease in the shop (sinks round 3, stage 3) ───────────────────


def test_shop_embed_voice_tier_only_when_priced(db):
    _enable(db)  # price_voice_style defaults to 0 — shipped dark
    embed = _build_shop_embed(_settings(db), set(), None, panel=True)
    assert not any(f.name == "Voice" for f in embed.fields)

    _enable(db, price_voice_style=30)
    embed = _build_shop_embed(_settings(db), set(), None, panel=True)
    voice = next(f for f in embed.fields if f.name == "Voice")
    assert "Voice" in voice.value and "30" in voice.value


@pytest.mark.asyncio
async def test_rent_voice_style_skips_role_projection(ctx, db):
    _enable(db, price_voice_style=30)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection() as (apply_mock, _r, _n):
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "voice_style")

    apply_mock.assert_not_awaited()  # no personal role involved
    msg = interaction.response.send_message.await_args.args[0]
    assert "Voice Style" in msg
    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["perk"] == "voice_style"
    assert rentals[0]["price"] == 30


@pytest.mark.asyncio
async def test_gift_voice_style_dark_refused_priced_allowed(ctx, db):
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="voice_style")
    assert "isn't active" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []

    _enable(db, price_voice_style=30)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _gift(cog, interaction, _member(member_id=900), perk="voice_style")
    apply_mock.assert_not_awaited()  # no role projection for a voice lease
    rentals = _live_rentals(db)
    assert len(rentals) == 1
    assert rentals[0]["perk"] == "voice_style"
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900


# ── /bank emoji guards (sinks round 3, stage 4) ──────────────────────────────


@pytest.mark.asyncio
async def test_bank_emoji_rejects_oversize_and_bad_type(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)

    big = MagicMock()
    big.content_type = "image/png"
    big.size = 300 * 1024
    interaction = _interaction(_member(member_id=500))
    await cog.bank_emoji.callback(cog, interaction, big, "party_blob")
    assert "256KB" in interaction.response.send_message.await_args.args[0]

    weird = MagicMock()
    weird.content_type = "image/tiff"
    weird.size = 1024
    interaction = _interaction(_member(member_id=500))
    await cog.bank_emoji.callback(cog, interaction, weird, "party_blob")
    assert "PNG" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_bank_emoji_disabled_at_price_zero(ctx, db):
    _enable(db, price_emoji=0)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    await cog.bank_emoji.callback(cog, interaction, None, None)
    assert "isn't enabled" in interaction.response.send_message.await_args.args[0]


# ── weekly raffle in the shop (sinks round 3, stage 5) ───────────────────────


def test_shop_embed_raffle_row_only_when_enabled(db):
    _enable(db)
    embed = _build_shop_embed(_settings(db), set(), None, panel=True)
    assert not any(f.name == "Weekly Raffle" for f in embed.fields)

    _enable(db, raffle_enabled=True)
    embed = _build_shop_embed(_settings(db), set(), None, panel=True)
    row = next(f for f in embed.fields if f.name == "Weekly Raffle")
    assert "10" in row.value  # ticket price


@pytest.mark.asyncio
async def test_buy_raffle_tickets_via_modal_flow(ctx, db):
    _enable(db, raffle_enabled=True)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_buy_raffle_tickets(interaction, _settings(db), "3")

    msg = interaction.response.send_message.await_args.args[0]
    assert "3 ticket(s)" in msg
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 70
        row = conn.execute(
            "SELECT count FROM econ_raffle_tickets WHERE user_id = 500"
        ).fetchone()
    assert row["count"] == 3


@pytest.mark.asyncio
async def test_buy_raffle_tickets_rejects_junk_quantity(ctx, db):
    _enable(db, raffle_enabled=True)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    await cog.do_buy_raffle_tickets(interaction, _settings(db), "lots")
    assert "whole number" in interaction.response.send_message.await_args.args[0]
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 100


# ── custom-name perk: renames the role AND the server nickname (#56) ───────────


def test_custom_name_confirmation_variants():
    ok = _custom_name_confirmation("Sir Fluffy", nick_ok=True)
    assert "Sir Fluffy" in ok
    assert "nickname" in ok.lower() and "role" in ok.lower()

    forbidden = _custom_name_confirmation(
        "Sir Fluffy", nick_ok=False, nick_reason=_NICK_FORBIDDEN
    )
    assert forbidden.startswith("Your role name is now **Sir Fluffy**.")
    assert "Manage Nicknames" in forbidden

    plain = _custom_name_confirmation("Sir Fluffy", nick_ok=False)
    assert plain == "Your role name is now **Sir Fluffy**."


@pytest.mark.asyncio
async def test_set_role_name_also_sets_nickname(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    actor.edit = AsyncMock()
    interaction = _interaction(actor)

    with (
        patch.object(
            cog,
            "_load_role_ctx",
            return_value=(EconSettings(enabled=True), {"role_name": True}, 0),
        ),
        patch.object(cog, "_name_blocklist", return_value=[]),
        patch.object(cog, "_upsert_role"),
        patch(
            "bot_modules.cogs.economy_cog.apply_role_perks",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.set_role_name(interaction, "Sir Fluffy")

    actor.edit.assert_awaited_once()
    assert actor.edit.await_args.kwargs.get("nick") == "Sir Fluffy"
    # _apply_and_confirm defers before the slow role-apply, then edits the
    # deferred response — the confirmation lands on edit_original_response.
    msg = interaction.edit_original_response.await_args.kwargs["content"]
    assert "Sir Fluffy" in msg and "nickname" in msg.lower()


@pytest.mark.asyncio
async def test_set_role_name_nick_forbidden_still_renames_role(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    resp = MagicMock(status=403, reason="Forbidden")
    actor.edit = AsyncMock(side_effect=discord.Forbidden(resp, "Missing Permissions"))
    interaction = _interaction(actor)
    upsert = MagicMock()

    with (
        patch.object(
            cog,
            "_load_role_ctx",
            return_value=(EconSettings(enabled=True), {"role_name": True}, 0),
        ),
        patch.object(cog, "_name_blocklist", return_value=[]),
        patch.object(cog, "_upsert_role", upsert),
        patch(
            "bot_modules.cogs.economy_cog.apply_role_perks",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.set_role_name(interaction, "Sir Fluffy")

    upsert.assert_called_once()  # the role rename still persists
    msg = interaction.edit_original_response.await_args.kwargs["content"]
    assert "Sir Fluffy" in msg
    assert "Manage Nicknames" in msg


# ── /bank pin (Pin of the Day) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pin_submit_refused_when_not_enabled(ctx, db):
    _enable(db)  # no price and no pin channel → the feature is off
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_pin_submit(interaction, "hello world")

    interaction.response.send_message.assert_awaited_once()
    assert "isn't enabled" in interaction.response.send_message.await_args.args[0]
    with open_db(db) as conn:
        rows = conn.execute("SELECT COUNT(*) c FROM econ_pin_submissions").fetchone()
    assert rows["c"] == 0  # nothing queued, nothing charged


@pytest.mark.asyncio
async def test_pin_submit_charges_and_queues(ctx, db):
    _enable(db, price_pin_of_day=30, pin_channel_id=5555)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 100, "grant", actor_id=1)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_pin_submit(interaction, "gm gamers")

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 70  # 30 held upfront
        row = conn.execute(
            "SELECT state, message FROM econ_pin_submissions WHERE guild_id = ?",
            (GUILD_ID,),
        ).fetchone()
    assert row["state"] == "pending"
    assert row["message"] == "gm gamers"
    interaction.followup.send.assert_awaited()  # the "sent for review" receipt


# ── /bounty (Community Bounty) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bounty_post_refused_when_not_enabled(ctx, db):
    _enable(db)  # no bounty channel → off
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_bounty_post(interaction, "do a thing", "", "50")

    interaction.response.send_message.assert_awaited_once()
    assert "aren't enabled" in interaction.response.send_message.await_args.args[0]
    with open_db(db) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM econ_bounties").fetchone()["c"]
    assert n == 0


@pytest.mark.asyncio
async def test_bounty_post_escrows_and_opens(ctx, db):
    _enable(db, bounty_channel_id=5555, bounty_min_stake=10)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 200, "grant", actor_id=1)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_bounty_post(interaction, "Draw the mascot", "as a knight", "50")

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 150  # 50 staked into the pot
        row = conn.execute(
            "SELECT state, poster_id, title FROM econ_bounties WHERE guild_id = ?",
            (GUILD_ID,),
        ).fetchone()
    assert row["state"] == "open"
    assert row["poster_id"] == 500
    assert row["title"] == "Draw the mascot"
    interaction.followup.send.assert_awaited()


@pytest.mark.asyncio
async def test_bounty_post_bad_stake_rejected(ctx, db):
    _enable(db, bounty_channel_id=5555, bounty_min_stake=10)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 200, "grant", actor_id=1)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_bounty_post(interaction, "Draw the mascot", "", "not-a-number")

    interaction.followup.send.assert_awaited()
    assert "whole number" in interaction.followup.send.await_args.args[0]
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 200  # nothing charged
