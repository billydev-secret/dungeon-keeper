"""Tests for the economy quest views — the /bank quests claim select and the
persistent sign-off Approve/Deny cards."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quest_views import (
    QuestApproveButton,
    QuestClaimView,
    QuestDenyButton,
    QuestDenyModal,
    QuestSignoffView,
    can_manage_economy,
)
from bot_modules.economy.quests import quest_period
from bot_modules.services.economy_quests_service import (
    claim_quest,
    create_quest,
    resolve_claim,
    set_quest_active,
)
from bot_modules.services.economy_service import (
    EconSettings,
    get_balance,
    load_econ_settings,
    save_econ_settings,
)
from migrations import apply_migrations_sync
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
BANK_CHANNEL = 424242
CLAIMANT = 500
MANAGER_ROLE = 7007


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
    with patch(
        "bot_modules.economy.quest_views.resolve_accent_color",
        new=AsyncMock(return_value=discord.Colour(0x123456)),
    ):
        yield


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True}
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _settings(db) -> EconSettings:
    with open_db(db) as conn:
        return load_econ_settings(conn, GUILD_ID)


def _member(*, admin=False, role_ids=(), member_id=CLAIMANT, premium=None) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = False
    m.display_name = "Member"
    m.mention = f"<@{member_id}>"
    m.premium_since = premium
    m.guild_permissions = MagicMock(administrator=admin)
    m.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return m


def _mk_quest(db, *, qtype="weekly", reward=30, signoff=1, title="Help out") -> int:
    with open_db(db) as conn:
        qid = create_quest(
            conn, GUILD_ID, title=title, description="", qtype=qtype, reward=reward,
            signoff=signoff, criteria="Do the thing", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=1,
        )
        set_quest_active(conn, GUILD_ID, qid, True)
    return qid


def _period(qtype="weekly") -> str:
    return quest_period(qtype, local_day_for(time.time(), 0))


def _pending_claim(db, qid, user_id=CLAIMANT, qtype="weekly") -> int:
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        outcome = claim_quest(
            conn, settings, GUILD_ID, qid, user_id, period=_period(qtype), booster=False
        )
    return outcome.claim_id


def _bot(ctx, *, claimant_premium=None) -> MagicMock:
    bot = MagicMock()
    bot.ctx = ctx
    guild = MagicMock()
    claimant = MagicMock()
    claimant.premium_since = claimant_premium
    guild.get_member = MagicMock(return_value=claimant)
    bot.get_guild = MagicMock(return_value=guild)
    return bot


def _button_interaction(bot, *, user, card=None) -> MagicMock:
    i = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    i.client = bot
    i.user = user
    i.message = card
    return i


# ── permission gate ───────────────────────────────────────────────────────────


def test_can_manage_admin_and_role():
    settings = EconSettings(manager_role_id=MANAGER_ROLE)
    assert can_manage_economy(_member(admin=True), settings)
    assert can_manage_economy(_member(role_ids=(MANAGER_ROLE,)), settings)
    assert not can_manage_economy(_member(role_ids=(1,)), settings)


# ── persistent custom_id round-trip (survives restart) ────────────────────────


@pytest.mark.asyncio
async def test_custom_id_roundtrip():
    approve = QuestApproveButton(12345)
    deny = QuestDenyButton(678)
    assert approve.custom_id == "econ_claim:approve:12345"
    assert deny.custom_id == "econ_claim:deny:678"

    am = approve.template.match("econ_claim:approve:12345")
    assert am is not None
    rebuilt = await QuestApproveButton.from_custom_id(MagicMock(), MagicMock(), am)
    assert rebuilt.claim_id == 12345

    dm = deny.template.match("econ_claim:deny:678")
    assert dm is not None
    rebuilt_d = await QuestDenyButton.from_custom_id(MagicMock(), MagicMock(), dm)
    assert rebuilt_d.claim_id == 678

    view = QuestSignoffView(99)
    assert view.timeout is None  # persistent
    assert {getattr(c, "custom_id", None) for c in view.children} == {
        "econ_claim:approve:99",
        "econ_claim:deny:99",
    }


# ── /bank quests claim select ─────────────────────────────────────────────────


def _claim_view(ctx, db, claimable) -> QuestClaimView:
    guild = cast(discord.Guild, FakeGuild(id=GUILD_ID))
    return QuestClaimView(ctx, _settings(db), guild, claimable)


@pytest.mark.asyncio
async def test_instant_claim_pays_once_and_relays_collision(ctx, db):
    _enable(db)
    qid = _mk_quest(db, qtype="daily", reward=15, signoff=0, title="Say hi")
    claimable = [{"id": qid, "title": "Say hi", "qtype": "daily", "reward": 15}]

    view = _claim_view(ctx, db, claimable)
    select = view.children[0]
    select._values = [str(qid)]  # type: ignore[attr-defined]

    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 15
    interaction.followup.send.assert_awaited_once()
    assert "embed" in interaction.followup.send.await_args.kwargs

    # Second claim same period → collision message relayed, no double pay.
    view2 = _claim_view(ctx, db, claimable)
    select2 = view2.children[0]
    select2._values = [str(qid)]  # type: ignore[attr-defined]
    interaction2 = _button_interaction(_bot(ctx), user=_member())
    await select2.callback(interaction2)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 15  # unchanged
    msg = interaction2.followup.send.await_args.args[0]
    assert "already completed" in msg.lower()


@pytest.mark.asyncio
async def test_signoff_claim_posts_card_and_records_ids(ctx, db):
    _enable(db, bank_channel_id=BANK_CHANNEL)
    qid = _mk_quest(db, qtype="weekly", reward=50, signoff=1, title="Sign me")
    claimable = [{"id": qid, "title": "Sign me", "qtype": "weekly", "reward": 50}]

    posted = MagicMock()
    posted.id = 9988
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = BANK_CHANNEL
    channel.send = AsyncMock(return_value=posted)
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_channel = MagicMock(return_value=channel)

    view = QuestClaimView(ctx, _settings(db), guild, claimable)
    select = view.children[0]
    select._values = [str(qid)]  # type: ignore[attr-defined]
    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)

    channel.send.assert_awaited_once()
    sent_view = channel.send.await_args.kwargs["view"]
    assert isinstance(sent_view, QuestSignoffView)
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT state, card_channel_id, card_message_id FROM econ_quest_claims "
            "WHERE quest_id = ?",
            (qid,),
        ).fetchone()
    assert row["state"] == "pending"
    assert row["card_channel_id"] == BANK_CHANNEL
    assert row["card_message_id"] == 9988
    # No payout yet — sign-off pending.
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 0


@pytest.mark.asyncio
async def test_signoff_claim_survives_missing_bank_channel(ctx, db):
    _enable(db)  # bank_channel_id unset (0)
    qid = _mk_quest(db, qtype="weekly", reward=50, signoff=1, title="No channel")
    claimable = [{"id": qid, "title": "No channel", "qtype": "weekly", "reward": 50}]
    view = _claim_view(ctx, db, claimable)
    select = view.children[0]
    select._values = [str(qid)]  # type: ignore[attr-defined]
    interaction = _button_interaction(_bot(ctx), user=_member())

    await select.callback(interaction)  # must not raise

    with open_db(db) as conn:
        row = conn.execute(
            "SELECT state FROM econ_quest_claims WHERE quest_id = ?", (qid,)
        ).fetchone()
    assert row["state"] == "pending"  # claim recorded despite no card
    interaction.followup.send.assert_awaited()


# ── approve / deny resolution ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_pays_dms_and_edits_card(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)

    card = MagicMock()
    card.edit = AsyncMock()
    bot = _bot(ctx, claimant_premium=None)
    interaction = _button_interaction(bot, user=_member(admin=True, member_id=999), card=card)

    with patch(
        "bot_modules.economy.quest_views.notify_member", new=AsyncMock(return_value=True)
    ) as notify:
        await QuestApproveButton(claim_id).callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 30
        state = conn.execute(
            "SELECT state, resolver_id FROM econ_quest_claims WHERE id = ?", (claim_id,)
        ).fetchone()
    assert state["state"] == "paid"
    assert state["resolver_id"] == 999
    card.edit.assert_awaited_once()
    edited = card.edit.await_args.kwargs["embed"]
    assert edited.colour == discord.Colour.green()
    assert card.edit.await_args.kwargs["view"] is None
    notify.assert_awaited_once()
    interaction.response.send_message.assert_awaited()  # ephemeral ack


@pytest.mark.asyncio
async def test_approve_uses_claimant_booster_not_managers(ctx, db):
    _enable(db)  # booster_multiplier 1.5
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)

    card = MagicMock()
    card.edit = AsyncMock()
    # Claimant IS boosting; the clicking manager is not — reward must boost.
    bot = _bot(ctx, claimant_premium=object())
    manager = _member(admin=True, member_id=999, premium=None)
    interaction = _button_interaction(bot, user=manager, card=card)

    with patch(
        "bot_modules.economy.quest_views.notify_member", new=AsyncMock(return_value=True)
    ):
        await QuestApproveButton(claim_id).callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 45  # ceil(30 * 1.5)


@pytest.mark.asyncio
async def test_deny_modal_stores_reason_dms_and_allows_reclaim(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)

    card = MagicMock()
    card.edit = AsyncMock()
    bot = _bot(ctx)
    interaction = _button_interaction(bot, user=_member(admin=True, member_id=999))

    modal = QuestDenyModal(claim_id, card)
    modal.reason = MagicMock(value="  not enough proof  ")  # type: ignore[assignment]

    with patch(
        "bot_modules.economy.quest_views.notify_member", new=AsyncMock(return_value=True)
    ) as notify:
        await modal.on_submit(interaction)

    with open_db(db) as conn:
        row = conn.execute(
            "SELECT state, deny_reason FROM econ_quest_claims WHERE id = ?", (claim_id,)
        ).fetchone()
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 0  # no pay on deny
    assert row["state"] == "denied"
    assert row["deny_reason"] == "not enough proof"
    card.edit.assert_awaited_once()
    assert card.edit.await_args.kwargs["embed"].colour == discord.Colour.red()
    notify.assert_awaited_once()
    assert notify.await_args is not None
    dm_embed = notify.await_args.kwargs["embed"]
    assert any("not enough proof" in f.value for f in dm_embed.fields)

    # Denied → re-claimable for the same period.
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        outcome = claim_quest(
            conn, settings, GUILD_ID, qid, CLAIMANT, period=_period(), booster=False
        )
    assert outcome.state == "pending"


@pytest.mark.asyncio
async def test_deny_button_refuses_non_manager_without_modal(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)
    bot = _bot(ctx)
    interaction = _button_interaction(bot, user=_member(role_ids=(1,)), card=MagicMock())

    await QuestDenyButton(claim_id).callback(interaction)

    interaction.response.send_modal.assert_not_awaited()
    msg = interaction.response.send_message.await_args.args[0]
    assert "permission" in msg.lower()


@pytest.mark.asyncio
async def test_approve_refuses_non_manager(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)
    card = MagicMock()
    card.edit = AsyncMock()
    bot = _bot(ctx)
    interaction = _button_interaction(bot, user=_member(role_ids=(1,)), card=card)

    await QuestApproveButton(claim_id).callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 0
    card.edit.assert_not_awaited()
    msg = interaction.response.send_message.await_args.args[0]
    assert "permission" in msg.lower()


@pytest.mark.asyncio
async def test_already_resolved_race_refreshes_card_no_double_pay(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)
    # Resolve it out-of-band (as the dashboard would) → paid.
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        resolve_claim(
            conn, settings, claim_id, approve=True, resolver_id=111, booster=False
        )
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 30

    card = MagicMock()
    card.edit = AsyncMock()
    bot = _bot(ctx)
    interaction = _button_interaction(bot, user=_member(admin=True, member_id=999), card=card)

    with patch(
        "bot_modules.economy.quest_views.notify_member", new=AsyncMock(return_value=True)
    ) as notify:
        await QuestApproveButton(claim_id).callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 30  # still single pay
    card.edit.assert_awaited_once()  # card refreshed to the true (paid) state
    notify.assert_not_awaited()  # no second DM
    msg = interaction.response.send_message.await_args.args[0]
    assert "already resolved" in msg.lower()
