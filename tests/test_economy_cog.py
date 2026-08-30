"""Cog-level tests for /bank — wallet view, mod grant matrix, and /bank quests."""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import (
    get_config_value,
    get_tz_offset_hours,
    open_db,
    set_config_value,
)
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quests import quest_period
from bot_modules.services.economy_quests_service import (
    claim_quest,
    create_quest,
    set_income_source,
    set_quest_active,
)
from bot_modules.cogs.economy_cog import (
    _NICK_FORBIDDEN,
    _custom_name_confirmation,
    _resolve_guild_emoji,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
    get_ledger,
    get_notify_muted,
    get_streak_shields,
    load_econ_settings,
    save_econ_settings,
)
from bot_modules.services.quote_renderer import THEMES
from bot_modules.services.voice_master_service import add_name_blocklist
from bot_modules.services.economy_shop_items_service import get_item
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, FakeRole, fake_interaction

GUILD_ID = 9001
MANAGER_ROLE_ID = 7007


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    # is_mod/member_is_mod answer False by default: these tests are about the
    # ordinary member's paid path, and the staff comp has its own coverage in
    # test_economy_rentals_logic / test_economy_mod_comp.
    def _set(key, value, guild_id=GUILD_ID):
        # core.role_provision persists a provisioned role id through this.
        with open_db(db) as conn:
            set_config_value(conn, key, value, guild_id)
            conn.commit()
        return value

    return SimpleNamespace(
        db_path=db,
        open_db=lambda: open_db(db),
        is_mod=lambda _interaction: False,
        member_is_mod=lambda _member: False,
        set_config_value=_set,
    )


@pytest.fixture(autouse=True)
def _patch_accent():
    """resolve_accent_color reads the guild avatar — stub it to a fixed color."""
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        yield


def _make_cog(ctx):
    from bot_modules.cogs.economy_cog import EconomyCog

    _bot = MagicMock()
    _bot.ctx = ctx
    return EconomyCog(_bot)


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


@pytest.mark.parametrize("command", ["wallet", "grant", "qotd", "quests", "mute"])
@pytest.mark.asyncio
async def test_disabled_economy_gate_per_command(ctx, db, command):
    # The disabled check is inlined per command, so each case exercises its
    # own gate. The "nothing written" invariants live in the service suites.
    cog = _make_cog(ctx)  # economy left disabled
    actor = _member(admin=True)  # admin, so only the gate can block
    if command == "qotd":
        interaction, channel = _qotd_interaction(actor)
        await _qotd(cog, interaction, "Blocked?")
        channel.send.assert_not_called()
    else:
        interaction = _interaction(actor)
        if command == "wallet":
            await _wallet(cog, interaction)
        elif command == "grant":
            await _grant(cog, interaction, _member(member_id=900), 10, "x")
        elif command == "quests":
            await _quests(cog, interaction)
        else:
            await _mute(cog, interaction)

    args = interaction.response.send_message.await_args.args
    kwargs = interaction.response.send_message.await_args.kwargs
    assert "enabled" in args[0].lower()
    assert kwargs["ephemeral"] is True
    assert "embed" not in kwargs


# ── /bank grant — permission matrix ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "actor_kind", "allowed"),
    [
        ("grant", "admin", True),
        ("grant", "manager", True),
        ("grant", "plain", False),
        ("qotd", "manager", True),
        ("qotd", "plain", False),
    ],
)
@pytest.mark.asyncio
async def test_mod_command_permission_matrix(ctx, db, command, actor_kind, allowed):
    _enable(db, manager_role_id=MANAGER_ROLE_ID)
    cog = _make_cog(ctx)
    actor = _member(
        admin=actor_kind == "admin",
        role_ids=(MANAGER_ROLE_ID,) if actor_kind == "manager" else (),
    )

    if command == "grant":
        interaction = _interaction(actor)
        await _grant(cog, interaction, _member(member_id=900), 10, "for helping")
        with open_db(db) as conn:
            acted = get_balance(conn, GUILD_ID, 900) == 10
    else:
        interaction, channel = _qotd_interaction(actor)
        await _qotd(cog, interaction, "Coffee or tea?")
        acted = channel.send.await_count == 1

    assert acted is allowed
    call = interaction.response.send_message.await_args
    if allowed and command == "grant":
        assert "embed" in call.kwargs
        assert call.kwargs.get("ephemeral") is not True  # public confirmation
    if not allowed:
        assert "permission" in call.args[0].lower()
        assert call.kwargs["ephemeral"] is True


# ── /bank grant — amounts and booster multiplier ─────────────────────────────


@pytest.mark.asyncio
async def test_grant_booster_target_gets_multiplier(ctx, db):
    _enable(db)  # default booster_multiplier == 1.5
    cog = _make_cog(ctx)
    actor = _member(admin=True)
    target = _member(member_id=900, premium=object())  # boosting
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 5, "boost love")

    # The ceil-rounding math itself is test_credit_booster_ceil_rounding
    # (service) — this covers the embed calling out the boost.
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert any("Booster" in f.name for f in embed.fields)


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
async def test_qotd_ping_role_set_to_none_posts_silently(ctx, db):
    """An admin who picked "(none)" keeps the silent post.

    A stored 0 is a preference, not an empty slot: provisioning over it would
    both delete the only way to say "don't ping" and start mentioning a role
    nobody holds. See docs/plans/role-autocreate.md.
    """
    _enable(db, qotd_ping_role_id=0)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    await _qotd(cog, interaction, "Quiet question?")

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] is None
    assert kwargs["allowed_mentions"].roles is False
    assert not interaction.guild.roles, "nothing should have been created"


@pytest.mark.asyncio
async def test_qotd_provisions_a_ping_role_when_never_configured(ctx, db):
    """A guild that never touched the dial gets @QOTD made for it."""
    _enable(db)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    await _qotd(cog, interaction, "First question?")

    made = list(interaction.guild.roles)
    assert [r.name for r in made] == ["QOTD"]
    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == f"<@&{made[0].id}>"
    # The id is persisted, so the next QOTD reuses it instead of making another.
    with open_db(db) as conn:
        assert get_config_value(
            conn, "econ_qotd_ping_role_id", "0", GUILD_ID
        ) == str(made[0].id)


@pytest.mark.asyncio
async def test_qotd_pings_configured_role(ctx, db):
    _enable(db, qotd_ping_role_id=4242)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    interaction.guild.roles[4242] = FakeRole(id=4242, name="QOTD")
    await _qotd(cog, interaction, "Loud question?")

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "<@&4242>"
    # Without this the mention posts as inert text. Allow-listed to exactly
    # this role rather than a blanket roles=True.
    assert [r.id for r in kwargs["allowed_mentions"].roles] == [4242]


@pytest.mark.asyncio
async def test_qotd_pings_on_card_path_too(ctx, db):
    """The ping rides on content, so it must survive the card branch."""
    _enable(db, qotd_ping_role_id=4242)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    interaction.guild.roles[4242] = FakeRole(id=4242, name="QOTD")
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
    assert [r.id for r in kwargs["allowed_mentions"].roles] == [4242]


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
    _mk_quest(
        db, qtype="monthly", reward=60, community_target=500,
        trigger_kind="message_sent", title="Monthly Marathon",
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
        conn.execute(
            "INSERT INTO econ_community_progress (quest_id, current) "
            "SELECT id, 200 FROM econ_quests WHERE title = 'Monthly Marathon'"
        )

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=user_id))
    await _quests(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    embed = kwargs["embed"]
    fields = {f.name: f.value or "" for f in embed.fields}
    # Two top-level sections now — the member's own board and the guild-wide
    # goals — not one field per cadence.
    assert [f.name for f in embed.fields] == ["🧍 Your quests", "🌐 Community goals"]
    personal = fields["🧍 Your quests"]
    community = fields["🌐 Community goals"]
    # Personal section: cadence sub-labels + one line per quest.
    assert "**Daily**" in personal and "**Weekly**" in personal
    assert "`Say hi" in personal and "🔶 claim below" in personal
    assert "`Weekly grind" in personal and "✅ done" in personal
    assert "`Sign me off" in personal and "⏳ sign-off" in personal
    # Community section: the monthly goal folds in beside the weekly community
    # goal, each under its cadence sub-label, with the ▰▱ bar — fill only, no
    # n/target counts (the shared five-figure totals live in the details popup).
    assert "**Monthly**" in community and "`Monthly Marathon" in community
    assert "**Weekly**" in community and "`Team goal" in community
    assert "▰" in community and "▱" in community
    assert "40/100" not in community and "200/500" not in community
    # The descriptions/explainers moved behind the details select — the
    # list never carries them.
    assert all("Do the thing" not in v for v in fields.values())
    # View always attaches when quests exist (details select at minimum).
    assert "view" in kwargs
    assert daily  # referenced


@pytest.mark.asyncio
async def test_quests_event_quests_have_no_display_section(ctx, db):
    # event ("Anytime") quests aren't board-drawn and don't get a section in
    # the personal panel — they stay a surprise payout rather than a
    # proactively-listed menu, even when dozens are active at once. With no
    # daily/weekly/community quests to fill the other sections, the embed
    # ends up with no fields at all — the 40 events are still reachable via
    # the details/claim select, just never proactively listed.
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
    assert not any(f.name in ("🧍 Your quests", "Anytime") for f in embed.fields)
    assert all("Anytime quest number" not in (f.value or "") for f in embed.fields)
    assert "view" in kwargs  # still claimable/detailable via the select


@pytest.mark.asyncio
async def test_cog_load_registers_persistent_buttons(ctx, db):
    from bot_modules.cogs.economy_cog import ShopRentButton
    from bot_modules.economy.bounty_views import (
        BountyAwardButton,
        BountyCancelButton,
        BountyChipInButton,
        BountyHubChipButton,
        BountyHubPostButton,
    )
    from bot_modules.economy.pin_views import PinApproveButton, PinDenyButton
    from bot_modules.economy.theme_views import ThemeApproveButton, ThemeDenyButton
    from bot_modules.economy.quest_views import QuestApproveButton, QuestDenyButton
    from bot_modules.economy.auction_views import AuctionBidButton
    from bot_modules.economy.sponsor_views import (
        SponsorApproveButton,
        SponsorDenyButton,
    )

    bot = MagicMock()
    cog = _make_cog(ctx)
    cog.bot = bot
    try:
        await cog.cog_load()
        # Every persistent button must be re-registered here or its custom_id
        # stops routing after a restart, leaving dead buttons on old messages.
        bot.add_dynamic_items.assert_called_once_with(
            QuestApproveButton,
            QuestDenyButton,
            ShopRentButton,
            SponsorApproveButton,
            SponsorDenyButton,
            PinApproveButton,
            PinDenyButton,
            ThemeApproveButton,
            ThemeDenyButton,
            BountyChipInButton,
            BountyAwardButton,
            BountyCancelButton,
            # The hub panel's two buttons — it is the board's only entry
            # point, so a dead hub means no way to post or chip in at all.
            BountyHubPostButton,
            BountyHubChipButton,
            AuctionBidButton,
        )
    finally:
        cog._auction_settle_loop.cancel()  # don't leak the background loop
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


# ── Stage 3: transfers / shop / role studio / gift / rentals ─────────────────


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


def _personal_role(db, user_id=500):
    """The member's personal-role row — what every role-studio setter writes."""
    with open_db(db) as conn:
        return conn.execute(
            "SELECT * FROM econ_personal_roles WHERE user_id = ?", (user_id,)
        ).fetchone()


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


# The confirm gate triggers *over* 100 (spec §5) — 100 itself sends straight
# through. One test for the threshold, one row per side of it.
@pytest.mark.parametrize(
    ("amount", "needs_confirm"),
    [
        pytest.param(50, False, id="under"),
        pytest.param(100, False, id="exactly-the-threshold"),
        pytest.param(200, True, id="over"),
    ],
)
@pytest.mark.asyncio
async def test_pay_confirm_gate_follows_the_threshold(ctx, db, amount, needs_confirm):
    from bot_modules.cogs.economy_cog import _PayConfirmView

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))

    with _patch_projection() as (_apply, _revoke, notify):
        await _pay(cog, interaction, _member(member_id=900, name="Bob"), amount)

    kwargs = interaction.response.send_message.await_args.kwargs
    with open_db(db) as conn:
        received = get_balance(conn, GUILD_ID, 900)

    if needs_confirm:
        assert isinstance(kwargs["view"], _PayConfirmView)
        assert received == 0  # the gate holds; nothing moved yet
        notify.assert_not_awaited()
    else:
        assert "view" not in kwargs
        assert received == amount
        notify.assert_awaited_once()
        assert str(amount) in notify.await_args.kwargs["content"]


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


# ── /bank pay public: ────────────────────────────────────────────────────────
#
# Rendering is tested in test_economy_transfers.py. What's tested here is the
# wiring the builder can't see: that the public receipt is actually *sent*, on
# both sides of the confirm threshold, and withheld on a no-contact pair.


def _postable(interaction) -> MagicMock:
    """Give a fake interaction a channel the bot may speak in."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.permissions_for.return_value = MagicMock(
        send_messages=True, embed_links=True
    )
    interaction.channel = channel
    interaction.guild.me = MagicMock()
    return channel


async def _pay_public(cog, interaction, member, amount, memo=None) -> None:
    await cog.bank_pay.callback(cog, interaction, member, amount, memo, True)


@pytest.mark.asyncio
async def test_public_pay_under_the_threshold_posts_and_withholds_balance(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))
    _postable(interaction)

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 40)

    # The sender's own reply stays ephemeral and carries the balance...
    ack = interaction.response.send_message.await_args
    assert ack.kwargs["ephemeral"] is True
    assert "460" in ack.args[0]

    # ...and the balance is exactly what the channel does not get.
    posted = interaction.followup.send.await_args.kwargs["embed"]
    assert posted.footer.text is None
    assert "460" not in str(posted.to_dict())
    assert "<@500>" in posted.description and "<@900>" in posted.description


@pytest.mark.asyncio
async def test_public_pay_over_the_threshold_posts_after_confirming(ctx, db):
    """The naive implementation does nothing here: an ephemeral confirm
    message cannot be edited into a public one, so the receipt has to be a
    separate send."""
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500, name="Alice")
    interaction = _interaction(sender)
    _postable(interaction)

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 200)
        confirm = interaction.response.send_message.await_args.kwargs["embed"]
        assert "posted in this channel" in confirm.description

        view = interaction.response.send_message.await_args.kwargs["view"]
        confirm_inter = _interaction(sender)
        _postable(confirm_inter)
        await view.children[0].callback(confirm_inter)

    # The ephemeral confirm resolves privately, with the balance...
    confirm_inter.response.edit_message.assert_awaited_once()
    assert "300" in confirm_inter.response.edit_message.await_args.kwargs["content"]
    # ...and the receipt reaches the channel on its own.
    posted = confirm_inter.followup.send.await_args.kwargs["embed"]
    assert posted.footer.text is None
    assert "<@500>" in posted.description and "<@900>" in posted.description


@pytest.mark.asyncio
async def test_public_pay_between_a_no_contact_pair_posts_nothing(ctx, db):
    """The payment still goes through, and the sender gets the ordinary
    private receipt — nothing distinguishes it from an unticked box."""
    from bot_modules.services.no_contact_service import add_pair

    _enable(db)
    _credit(db, 500, 500)
    add_pair(db, GUILD_ID, 500, 900, created_by=900)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))
    _postable(interaction)

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 40)

    interaction.followup.send.assert_not_awaited()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].footer.text == "Your balance: 460"
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 40


@pytest.mark.asyncio
async def test_public_pay_falls_back_quietly_where_the_bot_cannot_post(ctx, db):
    """Same silent downgrade as the no-contact case — that's what makes the
    no-contact case unremarkable."""
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))
    channel = _postable(interaction)
    channel.permissions_for.return_value = MagicMock(
        send_messages=False, embed_links=True
    )

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 40)

    interaction.followup.send.assert_not_awaited()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].footer.text == "Your balance: 460"


@pytest.mark.asyncio
async def test_public_option_never_publishes_a_refusal(ctx, db):
    """Announcing that someone is broke is a different feature."""
    _enable(db)
    _credit(db, 500, 10)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))
    _postable(interaction)

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 40)

    interaction.followup.send.assert_not_awaited()
    args, kwargs = interaction.response.send_message.await_args
    assert kwargs["ephemeral"] is True
    assert "don't have enough" in args[0]


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


# ── /bank shop ───────────────────────────────────────────────────────────────


async def _open_shop(cog, interaction) -> None:
    """Open the shop with every feature gate open — the default for these tests.

    Gate-closed behaviour has its own tests that patch the gate explicitly.
    """
    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, interaction)


async def _shop(cog, interaction) -> None:
    await cog.bank_shop.callback(cog, interaction)


def _offers(view):
    """What a section offers, whatever shape it rendered as.

    Two or more products become one ``_ActionSelect``; a lone one stays a
    button. Tests care about *what is on offer and whether it can be taken*,
    not which of the two shapes carried it — so this flattens both to
    ``{id: (label, refusal-or-"")}``.
    """
    from bot_modules.cogs.economy_cog import _ActionSelect

    out = {}
    for c in view.children:
        if isinstance(c, _ActionSelect):
            for value, (button, reason) in c._entries.items():
                out[value] = (str(button.label), reason)
        elif isinstance(c, discord.ui.Button) and getattr(c, "row", None) != 4:
            out[str(c.custom_id)] = (str(c.label), "")
    return out


def _entry(view, action_id):
    """The button behind one offer, whether it rendered as one or as an option."""
    from bot_modules.cogs.economy_cog import _ActionSelect

    for c in view.children:
        if isinstance(c, _ActionSelect) and action_id in c._entries:
            return c._entries[action_id][0]
        if isinstance(c, discord.ui.Button) and str(c.custom_id) == action_id:
            return c
    raise AssertionError(f"{action_id} is not on offer here")


def _unavailable(view):
    """The ids this section lists but cannot sell right now.

    Discord has no per-option disable, so an unavailable row keeps its place
    and explains itself when chosen — dropping it would make the picker
    disagree with the table above it.
    """
    return {k for k, (_label, reason) in _offers(view).items() if reason}


def _ShopView(*args, **kwargs):
    from bot_modules.cogs.economy_cog import _ShopView as view_cls

    return view_cls(*args, **kwargs)


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
    assert {i.split(":")[1] for i in _unavailable(view)} == {
        "role_gradient", "role_holographic", "role_icon",
    }
    blob = " ".join(f.value for f in kwargs["embed"].fields)
    assert "needs a server feature" in blob


@pytest.mark.asyncio
async def test_shop_buttons_carry_no_price(ctx, db):
    """Prices live in the table only, so re-pricing can't stale a button label."""
    _enable(db, price_role_name=35)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _open_shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    labels = [label for label, _ in _offers(kwargs["view"]).values()]
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

    await _open_shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    ids = set(_offers(kwargs["view"]))
    assert "econ_shop_cfg:role_color" in ids
    assert "econ_shop_rent:role_color" not in ids
    # The other perks still offer Rent.
    assert "econ_shop_rent:role_name" in ids
    # The rented row is ticked in the table.
    table = "\n".join(f.value for f in kwargs["embed"].fields)
    assert any(
        ln.startswith("`Color") and "✅" in ln for ln in table.splitlines()
    )


@pytest.mark.asyncio
async def test_shop_rented_holographic_shows_active_not_customise(ctx, db):
    """Holographic has no member styling, so its rented row is an inert chip."""
    _enable(db)
    _add_rental(db, "role_holographic")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _open_shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    by_id = _offers(kwargs["view"])
    # Shown as active + disabled — no customise modal, no rent button.
    assert "econ_shop_active:role_holographic" in by_id
    assert by_id["econ_shop_active:role_holographic"][1]  # refuses if chosen
    assert "econ_shop_cfg:role_holographic" not in by_id
    assert "econ_shop_rent:role_holographic" not in by_id


@pytest.mark.asyncio
async def test_shop_customise_button_opens_modal(ctx, db):
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _open_shop(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    button = _entry(view, "econ_shop_cfg:role_color")
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

    await _open_shop(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    ids = set(_offers(view))
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
    row = _personal_role(db)
    assert row["name"] == "Stardust"


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
    """Entry point is post_shop_panel(guild, channel) now — /bank post-shop was
    replaced by Economy → Settings → Post to Discord on 2026-07-28."""
    _enable(db)
    cog = _make_cog(ctx)
    channel = _panel_channel()

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.post_shop_panel(FakeGuild(id=GUILD_ID), channel)

    kwargs = channel.send.await_args.kwargs
    assert kwargs["embed"].title == "🛍️ Perk Shop"
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
    old = MagicMock(edit=AsyncMock(), delete=AsyncMock(), id=4444)
    channel.get_partial_message = MagicMock(return_value=old)

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.post_shop_panel(FakeGuild(id=GUILD_ID), channel)

    assert "view" in old.edit.await_args.kwargs  # re-priced labels ride along
    channel.send.assert_not_awaited()
    assert _shop_panel_stored(db) == (777, 4444)


# The "plain member is refused" case moved with the command. post_shop_panel is
# unguarded by design — its only caller is POST /api/panels/{key}/post, which is
# admin-gated by the route (and covered by tests/web/test_panels_routes.py plus
# the authz sweep). Re-checking permissions in the cog would duplicate a gate
# that now has exactly one door.


# ── shop panel sticky (keep it at the channel bottom) ────────────────────────


def _shop_listener_msg(*, author_bot: bool, channel_id: int, message_id: int):
    m = MagicMock(spec=discord.Message)
    m.guild = FakeGuild(id=GUILD_ID)
    m.author = MagicMock(bot=author_bot)
    m.channel = SimpleNamespace(id=channel_id)
    m.id = message_id
    return m


# The shop panel's sticky machinery is core.sticky.StickyPanel, covered
# generically by tests/test_core_sticky.py (debounce, bot-message skip,
# post-before-delete, never-create-on-restick). What remains economy-specific
# is which settings fields it reads and the disabled-economy gate.


@pytest.mark.asyncio
async def test_shop_panel_ids_read_the_shop_fields(ctx, db):
    _enable(db, shop_channel_id=111, shop_message_id=4444)
    cog = _make_cog(ctx)
    assert cog._panel_ids(GUILD_ID, "shop") == (111, 4444)


@pytest.mark.asyncio
async def test_shop_panel_ids_are_zero_when_the_economy_is_disabled(ctx, db):
    """A disabled economy reads as "no panel", so the shop never re-sticks."""
    _enable(db, shop_channel_id=111, shop_message_id=4444)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": "0"})
    cog = _make_cog(ctx)
    assert cog._panel_ids(GUILD_ID, "shop") == (0, 0)


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
    # This guild stocks nothing of its own, so the book opens on its first
    # real section rather than on a Specials page it has no items for.
    assert kwargs["embed"].title == "🎨 Role cosmetics"
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
    disabled = _unavailable(kwargs["view"])
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
async def test_require_perk_falls_back_for_an_unmapped_perk(ctx, db):
    """A setter for a perk with no _PERK_REFUSAL row still refuses politely.

    SELF_PERKS has six members and the refusal table four, so a future setter
    for role_holographic or voice_style would otherwise raise KeyError and
    show the member "This interaction failed."
    """
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))

    assert await cog._require_perk(interaction, GUILD_ID, "role_holographic") is False
    msg = interaction.response.send_message.await_args.args[0]
    assert "Rent that perk first" in msg


def test_disabled_shop_stops_after_the_settings_row(ctx, db):
    """The economy-off gate stays cheap — no catalog/rental reads to say 'off'.

    The returned values would be empty either way; what this pins is that the
    four post-gate queries aren't issued for a page that will only ever say
    the economy is disabled.
    """
    cog = _make_cog(ctx)  # economy left disabled
    with (
        patch("bot_modules.cogs.economy_cog.catalog_price_range") as catalog,
        patch("bot_modules.cogs.economy_cog.list_refundable_rentals") as rentals,
    ):
        shop = cog._shop_context(GUILD_ID, 500)

    assert shop.settings.enabled is False
    catalog.assert_not_called()
    rentals.assert_not_called()
    assert (shop.owned, shop.balance, shop.icon_range) == (set(), 0, None)
    assert (shop.refundable, shop.shields_held, shop.shield_price) == ([], 0, 0)


@pytest.mark.parametrize(
    ("perk", "rented", "refusal"),
    [
        # No rental at all → the entitlement check refuses.
        ("role_name", False, "rent"),
        ("role_color", False, "perk"),
        # Rented, but the server feature gate is closed → refused at the gate
        # (role_name/role_color aren't feature-gated, so the closed gate below
        # never touches them).
        ("role_gradient", True, "support"),
        ("role_icon", True, "support"),
    ],
)
@pytest.mark.asyncio
async def test_role_studio_setter_refusals(ctx, db, perk, rented, refusal):
    _enable(db)
    if rented:
        _add_rental(db, perk)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch(
            "bot_modules.cogs.economy_cog.feature_gate_ok",
            new=AsyncMock(return_value=False),
        ),
    ):
        if perk == "role_name":
            await _role_name(cog, interaction, "Cool")
        elif perk == "role_color":
            await _role_color(cog, interaction, "#7B2FF7")
        elif perk == "role_gradient":
            await _role_gradient(cog, interaction, "#111111", "#222222")
        else:
            await _role_icon_emoji(cog, interaction, ":party:")
    assert refusal in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


# ── staff perk comp ────────────────────────────────────────────────────
#
# The comp's own decision table lives in test_economy_rentals_logic; these
# two are the wiring — that the cog's staff verdict actually reaches the
# entitlement read, in both directions. Without them the gate could ignore
# ``is_mod`` entirely and every pure test would still pass.


@pytest.mark.asyncio
async def test_role_setter_comped_for_staff_without_any_rental(ctx, db):
    _enable(db, mod_perk_comp=True)
    ctx.is_mod = lambda _interaction: True
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#00FF00")
    apply_mock.assert_awaited_once()
    assert _personal_role(db)["color"] == 0x00FF00
    # A comp is not a purchase: no rental row was created behind it.
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_role_setter_still_refuses_staff_while_comp_is_off(ctx, db):
    """The dashboard switch is enforced, not decorative."""
    _enable(db)  # mod_perk_comp defaults off
    ctx.is_mod = lambda _interaction: True
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#00FF00")
    assert "perk" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.parametrize(
    ("was_staff", "now_staff", "comp_on", "applies", "revokes"),
    [
        # Promoted → project the comped perks onto their personal role.
        (False, True, True, True, False),
        # Demoted → re-project, which reverts the nick and deletes the role
        # when no real rental is left behind the comp.
        (True, False, True, False, True),
        # Staff-ness unchanged (a nick edit, an unrelated role) → no work.
        (True, True, True, False, False),
        (False, False, True, False, False),
        # Comp switched off for this guild → the listener stays out of it.
        (False, True, False, False, False),
    ],
)
@pytest.mark.asyncio
async def test_staff_change_reprojects_perks(
    ctx, db, was_staff, now_staff, comp_on, applies, revokes
):
    _enable(db, mod_perk_comp=comp_on)
    ctx.member_is_mod = lambda member: member.staff
    cog = _make_cog(ctx)
    before = _member(member_id=500)
    after = _member(member_id=500)
    before.staff, after.staff = was_staff, now_staff
    # A role diff is the listener's cheap pre-filter; give it one so the
    # staff-ness comparison is what decides these cases.
    before.roles = [SimpleNamespace(id=1)]
    after.roles = [SimpleNamespace(id=2)]
    for m in (before, after):
        m.guild = SimpleNamespace(id=GUILD_ID)
    with _patch_projection() as (apply_mock, revoke_mock, _n):
        await cog._on_staff_comp_changed(before, after)
    assert apply_mock.await_count == (1 if applies else 0)
    assert revoke_mock.await_count == (1 if revokes else 0)


@pytest.mark.asyncio
async def test_staff_change_ignores_updates_that_touch_no_roles(ctx, db):
    """The listener sees every nick/avatar/timeout edit — exit before any I/O."""
    _enable(db, mod_perk_comp=True)
    ctx.member_is_mod = MagicMock(return_value=True)
    cog = _make_cog(ctx)
    roles = [SimpleNamespace(id=1)]
    before, after = _member(member_id=500), _member(member_id=500)
    before.roles = after.roles = roles
    with _patch_projection() as (apply_mock, revoke_mock, _n):
        await cog._on_staff_comp_changed(before, after)
    ctx.member_is_mod.assert_not_called()
    apply_mock.assert_not_awaited()
    revoke_mock.assert_not_awaited()


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
    row = _personal_role(db)
    assert row["name"] == "Stardust"


@pytest.mark.asyncio
async def test_role_color_bad_hex(ctx, db):
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection():
        await _role_color(cog, interaction, "not-a-color")
    assert "hex" in interaction.response.send_message.await_args.args[0].lower()


def _staff_role(color=0xFF0000, name="Admins"):
    staff = MagicMock()
    staff.id = 77
    staff.name = name
    staff.color = discord.Color(color)
    staff.permissions = discord.Permissions(administrator=True)
    return staff


@pytest.mark.parametrize(
    "picked",
    [
        pytest.param("#FE0101", id="near-identical-red"),
        pytest.param("#FF0000", id="exactly-the-staff-color"),
    ],
)
@pytest.mark.asyncio
async def test_role_color_matching_a_staff_role_is_allowed(ctx, db, picked):
    """The ΔE staff-collision guard is gone (2026-07-29) — this is the new rule.

    It refused any color within ΔE 25 of a moderation-permissioned role, on
    the grounds that a matching hue could let a member pass for staff in the
    member list. Every staff role in this server now carries a role icon,
    which distinguishes staff regardless of hue, so the color no longer has to.
    Any parseable hex is accepted, up to and including an exact match.
    """
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), roles=[_staff_role()])
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, picked)
    apply_mock.assert_awaited_once()
    assert _personal_role(db)["color"] == int(picked.lstrip("#"), 16)


@pytest.mark.asyncio
async def test_role_gradient_matching_a_staff_role_is_allowed(ctx, db):
    """Both stops were checked, so both had to stop being checked."""
    _enable(db)
    _add_rental(db, "role_gradient")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), roles=[_staff_role()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch(
            "bot_modules.cogs.economy_cog.feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _role_gradient(cog, interaction, "#FF0000", "#FE0101")
    apply_mock.assert_awaited_once()
    row = _personal_role(db)
    assert (row["color"], row["color2"]) == (0xFF0000, 0xFE0101)


@pytest.mark.asyncio
async def test_role_color_gift_entitlement_allows(ctx, db):
    _enable(db)
    _add_rental(db, "role_color", user_id=800, beneficiary_id=500)  # gifted to 500
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#00FF00")
    apply_mock.assert_awaited_once()
    row = _personal_role(db)
    assert row["color"] == 0x00FF00


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
    row = _personal_role(db)
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
async def test_pick_custom_icon_is_free_for_comped_staff(ctx, db):
    """The picker's Custom entry reaches the rent flow directly, so the comp
    has to be enforced there and not just on the shop's hidden buttons."""
    _enable(db, mod_perk_comp=True)
    _credit(db, 500, 500)
    ctx.is_mod = lambda _interaction: True
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
    assert _live_rentals(db) == []
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500  # not a coin
    apply_mock.assert_awaited_once()
    assert "Unlocked" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_pick_catalog_icon_is_free_for_comped_staff(ctx, db):
    """A catalog icon IS the role_icon perk at a per-icon price, so the comp
    covers it — otherwise the curated icons would be the one thing a mod
    still had to buy."""
    from bot_modules.services.economy_icon_catalog_service import add_catalog_icon

    _enable(db, mod_perk_comp=True)
    _credit(db, 500, 500)
    ctx.is_mod = lambda _interaction: True
    from bot_modules.services.economy_icon_catalog_service import (
        set_catalog_icon_image,
    )

    with open_db(db) as conn:
        icon_id = add_catalog_icon(conn, GUILD_ID, name="Crown", price=100)
        set_catalog_icon_image(conn, GUILD_ID, icon_id, "/icons/crown.png")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch(
            "bot_modules.cogs.economy_cog.feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_catalog_icon(interaction, _guild_roles(), icon_id)
    assert _live_rentals(db) == []
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500
        row = conn.execute(
            "SELECT icon_path FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["icon_path"] == "/icons/crown.png"  # the art still landed
    apply_mock.assert_awaited_once()
    assert "on the house" in interaction.edit_original_response.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_pick_catalog_icon_still_charges_a_non_mod(ctx, db):
    from bot_modules.services.economy_icon_catalog_service import add_catalog_icon

    _enable(db, mod_perk_comp=True)
    _credit(db, 500, 500)
    with open_db(db) as conn:
        icon_id = add_catalog_icon(conn, GUILD_ID, name="Crown", price=100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        patch(
            "bot_modules.cogs.economy_cog.feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_catalog_icon(interaction, _guild_roles(), icon_id)
    assert len(_live_rentals(db)) == 1
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 400


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
    row = _personal_role(db)
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


# The shop table (economy/shop.py) and the shop buttons (_ShopView, here)
# decide row visibility from the same three prices, independently and in two
# different modules. A guild that prices one of them at 0 must get neither the
# row nor the button — never a listed row you can't buy, or a button for a row
# that isn't there. Both halves read dashboard-editable knobs, so this drift is
# reachable in prod, not theoretical.
@pytest.mark.parametrize(
    ("overrides", "field", "custom_id"),
    [
        pytest.param({"price_voice_style": 0}, "Voice", "econ_shop_rent:voice_style",
                     id="voice-dark"),
        pytest.param({"price_voice_style": 30}, "Voice", "econ_shop_rent:voice_style",
                     id="voice-priced"),
        pytest.param({"price_streak_shield": 0}, "One-shot", "econ_shop_shield",
                     id="shield-off"),
        pytest.param({"price_streak_shield": 40}, "One-shot", "econ_shop_shield",
                     id="shield-priced"),
        pytest.param({"raffle_enabled": False}, "Weekly Raffle", "econ_shop_raffle",
                     id="raffle-off"),
        pytest.param({"raffle_enabled": True}, "Weekly Raffle", "econ_shop_raffle",
                     id="raffle-on"),
    ],
)
@pytest.mark.asyncio
async def test_shop_table_row_and_its_button_agree(ctx, db, overrides, field, custom_id):
    _enable(db, **overrides)
    _credit(db, 500, 5000)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _open_shop(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    has_row = any(f.name == field for f in kwargs["embed"].fields)
    has_button = any(
        str(b.custom_id) == custom_id
        for b in kwargs["view"].children
        if isinstance(b, discord.ui.Button)
    )
    assert has_row is has_button, (
        f"{field!r} row={has_row} but {custom_id!r} button={has_button} "
        f"for {overrides}"
    )


# ── /bank gift ───────────────────────────────────────────────────────────────


async def _gift(cog, interaction, member, perk="role_color") -> None:
    # A bare string, not an app_commands.Choice: the perk parameter moved to
    # autocomplete so the picker can be per-guild (a switched-off perk must
    # not be offered), and autocomplete parameters arrive as their raw value.
    await cog.bank_gift.callback(cog, interaction, member, perk)


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


@pytest.mark.parametrize(
    ("command", "target_kind", "refusal"),
    [
        ("grant", "bot", "bot"),
        ("pay", "bot", "bot"),
        ("gift", "bot", "bot"),
        ("gift", "self", "your own"),
    ],
)
@pytest.mark.asyncio
async def test_self_and_bot_targets_rejected(ctx, db, command, target_kind, refusal):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    actor = _member(member_id=500, admin=True)  # admin so grant reaches its guard
    target = actor if target_kind == "self" else _member(member_id=901, is_bot=True)
    interaction = _interaction(actor)

    with _patch_projection():
        if command == "grant":
            await _grant(cog, interaction, target, 10, "x")
        elif command == "pay":
            await _pay(cog, interaction, target, 50)
        else:
            await _gift(cog, interaction, target)

    call = interaction.response.send_message.await_args
    assert refusal in call.args[0].lower()
    assert call.kwargs["ephemeral"] is True
    assert _live_rentals(db) == []  # no gift rental opened
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 901) == 0  # nothing credited


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
    # Discord's 1024-char cap and 400 the whole wallet embed; fit_lines trims.
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
async def test_signoff_trigger_quest_boards_the_claim_and_reacts_only(ctx, db):
    """A sign-off quest still files the claim and puts it on the mods' board,
    but the member only gets the 📝 reaction — no reply, no DM, regardless of
    game_role_id."""
    _enable(db, game_role_id=777)
    _mk_quest(db, reward=10, signoff=1, trigger_words="did it")
    cog = _make_cog(ctx)

    for member in (_member(member_id=501, role_ids=(777,)), _member(member_id=502)):
        msg = _trigger_message(author=member, content="did it")
        with (
            patch(
                "bot_modules.cogs.economy_cog.refresh_signoff_board", new=AsyncMock()
            ) as board,
            patch(
                "bot_modules.cogs.economy_cog.notify_member",
                new=AsyncMock(return_value=True),
            ) as notify,
        ):
            await cog._on_trigger_message(msg)

        board.assert_awaited_once()  # the claim shows up on the todo board
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
async def test_trigger_signoff_quest_files_pending_claim_and_boards_it(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=10, signoff=1, trigger_words="did it")
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _trigger_message(author=member, content="I did it!")
    with patch(
        "bot_modules.cogs.economy_cog.refresh_signoff_board", new=AsyncMock()
    ) as board:
        await cog._on_trigger_message(msg)

    assert _balance(db, 501) == 0  # sign-off gates the payout
    board.assert_awaited_once()
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
    assert loaded.price_quest_reroll == 10  # untouched → default


# ── photo-post event quest ──────────────────────────────────────────────────

PHOTO_CHANNEL_ID = 111


def _disable_photo_source(db) -> None:
    with open_db(db) as conn:
        set_income_source(conn, GUILD_ID, "photo_post", False)
        conn.commit()


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


@pytest.mark.parametrize(
    ("setup", "channel_id", "parent_id", "pays"),
    [
        # A THREAD of the Photo Challenge channel earns too: the listener
        # matches on parent_id, not just the exact channel id (mirrors the
        # trigger-quest / games siblings).
        ("config", 999_888, PHOTO_CHANNEL_ID, True),
        # Neither the scoped channel nor a thread of it pays nothing — the
        # parent_id widening must not swallow the whole guild.
        ("config", 999_888, 555_444, False),
        # A finished (status='done') schedule is not a live channel — the
        # fallback only recovers the channel from an *active* schedule.
        ("done_schedule", PHOTO_CHANNEL_ID, None, False),
    ],
)
@pytest.mark.asyncio
async def test_photo_post_channel_scoping(ctx, db, setup, channel_id, parent_id, pays):
    _enable(db)  # reward_photo_post defaults to 5
    if setup == "config":
        _set_photo_config(db)  # scoped channel is PHOTO_CHANNEL_ID
    else:
        _set_photo_schedule(db, status="done")  # no config row at all
    cog = _make_cog(ctx)

    msg = _photo_msg(
        author=_member(member_id=501), channel_id=channel_id, parent_id=parent_id
    )
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == (5 if pays else 0)
    if pays:
        msg.add_reaction.assert_awaited_once_with("✅")
    else:
        msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_post_no_payout_when_source_disabled(ctx, db):
    # The photo_post income-source toggle gates both payouts. The
    # participation-award half of this gate lives only in the cog listener
    # (economy_cog._on_photo_post) with no service-layer equivalent, so this
    # is its sole enforcement test — a dashboard toggle must never ship
    # unenforced (restored after the 2026-07 dedup removed it as a supposed
    # service-test duplicate; only the quest-bonus half was covered there).
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
        "bot_modules.cogs.economy_cog.refresh_signoff_board", new=AsyncMock()
    ) as board:
        await cog._on_photo_post(msg)

    assert _balance(db, 501) == 0  # sign-off gates the payout
    board.assert_awaited_once()
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


async def _pay(cog, interaction, member, amount, memo=None) -> None:
    await cog.bank_pay.callback(cog, interaction, member, amount, memo)


@pytest.mark.asyncio
async def test_pay_memo_reaches_embed_and_dm(ctx, db):
    # Ledger persistence of the memo is the service's job
    # (test_transfer_memo_lands_on_both_ledger_sides) — this covers the
    # embed/DM wiring on the cog side.
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


@pytest.mark.asyncio
async def test_shop_view_shield_button_disabled_while_held(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    from bot_modules.economy.shop import SECTION_GAMES

    view = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(),
        section=SECTION_GAMES,
    )
    # On offer and takeable.
    assert not _offers(view)["econ_shop_shield"][1]
    held = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(), shields_held=1,
        section=SECTION_GAMES,
    )
    # Still listed — the table above still shows the row — but it says why it
    # can't be bought again rather than silently doing nothing.
    assert "already holding one" in _offers(held)["econ_shop_shield"][1]


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
    # Single-charge enforcement is test_purchase_shield_refused_while_holding
    # (service) — this covers the user-facing copy.
    assert "already holding" in interaction.response.send_message.await_args.args[0]


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


def _scoped_view(name, cog, settings):
    """Build one member-scoped view, owned by member 500."""
    from bot_modules.cogs import economy_cog as mod

    owner, friend = _member(member_id=500), _member(member_id=501)
    guild = _guild_roles()
    return {
        "_RefundPickerView": lambda: mod._RefundPickerView(
            cog, settings, guild, 500, [], 30
        ),
        "_PayConfirmView": lambda: mod._PayConfirmView(
            cog, settings, guild, owner, friend, 250
        ),
        "_GiftConfirmView": lambda: mod._GiftConfirmView(
            cog, settings, guild, owner, friend, "role_color"
        ),
        "_EmojiCancelView": lambda: mod._EmojiCancelView(cog, 7, 500),
        "_RefundConfirmView": lambda: mod._RefundConfirmView(
            cog, settings, guild, 500, "role_color"
        ),
    }[name]()


# Every scoped view routes its ownership guard through _MemberScopedView; the
# refusal copy is per-class, so pin each one. Before the shared base these were
# five hand-rolled interaction_checks and only the picker had a test.
@pytest.mark.parametrize(
    ("view_name", "denial"),
    [
        ("_RefundPickerView", "Open your own shop"),
        ("_PayConfirmView", "This confirmation isn't yours."),
        ("_GiftConfirmView", "This confirmation isn't yours."),
        ("_EmojiCancelView", "This isn't your submission."),
        ("_RefundConfirmView", "This confirmation isn't yours."),
    ],
)
@pytest.mark.asyncio
async def test_scoped_views_reject_other_members(ctx, db, view_name, denial):
    cog = _make_cog(ctx)
    view = _scoped_view(view_name, cog, _settings(db))

    stranger = _interaction(_member(member_id=999))
    assert await view.interaction_check(stranger) is False
    assert denial in stranger.response.send_message.await_args.args[0]

    owner = _interaction(_member(member_id=500))
    assert await view.interaction_check(owner) is True


@pytest.mark.asyncio
async def test_refund_picker_previews_prorated_amount(ctx, db):
    _enable(db)
    _credit(db, 500, 200)
    _add_rental(db, "role_color", user_id=500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    shop = cog._shop_context(GUILD_ID, 500)

    await cog.open_refund_picker(
        interaction, _settings(db), _guild_roles(), shop.refundable, shop.shield_price
    )
    view = interaction.response.send_message.await_args.kwargs["view"]
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))
    assert len(select.options) == 1
    assert select.options[0].value.startswith("rental:")
    assert "back" in select.options[0].description


@pytest.mark.asyncio
async def test_refund_confirm_revokes_perk_and_confirms(ctx, db):
    # The prorate math and the rental ending now are the rentals service's
    # job — this covers the cog wiring: the perk revoke fires and the picker
    # message is edited with the confirmation.
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
        "bot_modules.cogs.economy_cog.revoke_perk_effect", new=AsyncMock()
    ) as revoke_mock:
        await cog.finalize_refund(
            interaction, _settings(db), _guild_roles(), f"rental:{rid}"
        )
    revoke_mock.assert_awaited_once()
    text = interaction.response.edit_message.await_args.kwargs["content"]
    assert "credited back" in text


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


# ── voice-style lease in the shop (sinks round 3, stage 3) ───────────────────


@pytest.mark.asyncio
async def test_rent_voice_style_skips_role_projection(ctx, db):
    # Both dials: pricing the lease no longer implies selling it.
    _enable(db, price_voice_style=30, shop_voice_style_enabled=True)
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
    """Gifting follows the Shop & Perks checkbox, not the price.

    The refusal used to hang off ``price_voice_style <= 0``; it is now the
    generic "is this on sale here" guard, so pricing the lease is no longer
    enough on its own — the box has to be checked.
    """
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="voice_style")
    assert "isn't for sale" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []

    # Priced but still unchecked: the shop doesn't sell it, so neither does
    # the gift route.
    _enable(db, price_voice_style=30)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="voice_style")
    assert "isn't for sale" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []

    _enable(db, price_voice_style=30, shop_voice_style_enabled=True)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _gift(cog, interaction, _member(member_id=900), perk="voice_style")
    apply_mock.assert_not_awaited()  # no role projection for a voice lease
    rentals = _live_rentals(db)
    assert len(rentals) == 1
    assert rentals[0]["perk"] == "voice_style"
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900


@pytest.mark.asyncio
async def test_gift_autocomplete_offers_only_what_the_guild_sells(ctx, db):
    """The wiring reason the perk parameter stopped being a static Choice list.

    ``@app_commands.choices`` is baked in at import and identical in every
    guild, so a switched-off perk would keep appearing in the picker and only
    refuse after someone chose it.
    """
    _enable(db, shop_role_gradient_enabled=False, shop_voice_style_enabled=True)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    choices = await cog._giftable_autocomplete(interaction, "")
    values = [c.value for c in choices]
    assert "role_gradient" not in values
    assert "role_color" in values and "voice_style" in values


@pytest.mark.asyncio
async def test_gift_autocomplete_filters_by_what_was_typed(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    choices = await cog._giftable_autocomplete(interaction, "gradient")
    assert [c.value for c in choices] == ["role_gradient"]


@pytest.mark.asyncio
async def test_gift_refuses_a_perk_that_never_appeared_in_the_picker(ctx, db):
    # Autocomplete is a convenience, not a gate: Discord delivers whatever the
    # client sends, including a hand-typed value.
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="not_a_perk")
    assert "isn't a perk" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_gift_refused_for_a_perk_the_guild_switched_off(ctx, db):
    _enable(db, shop_role_gradient_enabled=False)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="role_gradient")
    assert "isn't for sale" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_rent_refused_for_a_perk_the_guild_switched_off(ctx, db):
    _enable(db, shop_role_color_enabled=False)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "role_color")
    assert "stopped selling" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_a_comped_mod_cannot_claim_a_switched_off_perk(ctx, db):
    """The bypass this guards: the comp path skips ``rent_perk`` entirely.

    A comped moderator never reaches the service-layer refusal, so a stale
    shop button would have handed them a perk the server had switched off —
    and moderators are exactly the people looking at a shop embed left open
    while they change the settings.
    """
    _enable(db, mod_perk_comp=True, shop_role_color_enabled=False)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    ctx.is_mod = lambda _i: True
    with _patch_projection() as (apply_mock, _r, _n):
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "role_color")
    assert "stopped selling" in interaction.response.send_message.await_args.args[0]
    apply_mock.assert_not_awaited()  # no role projected either


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


# ── role icon: resolving a custom emoji by the short name typed (#89) ─────────


class _FakeEmoji:
    """Just the attributes ``_resolve_guild_emoji`` reads off a real emoji."""

    def __init__(self, name: str, id: int, animated: bool = False):
        self.name, self.id, self.animated = name, id, animated


class _FakeGuild:
    def __init__(self, *emojis: _FakeEmoji):
        self.emojis = list(emojis)


def _emoji_guild() -> _FakeGuild:
    # Mixed-case names are the norm on an emote-heavy server — that's what
    # made this bug hit "some" emojis and not others.
    return _FakeGuild(
        _FakeEmoji("KEKW", 1),
        _FakeEmoji("monkaS", 2),
        _FakeEmoji("peepoHappy", 3),
        _FakeEmoji("pepe", 4),
        _FakeEmoji("dance", 5, animated=True),
    )


@pytest.mark.parametrize(
    "raw, expected_id",
    [
        # The casing the member actually types, vs the server's stored casing.
        (":kekw:", 1),
        (":KEKW:", 1),
        ("KEKW", 1),
        (":monkas:", 2),
        (":monkaS:", 2),
        (":peepohappy:", 3),
        (":PEEPOHAPPY:", 3),
        # Already-lowercase names worked before the fix and must keep working.
        (":pepe:", 4),
        # Animated resolves here; the caller is what refuses it.
        (":DANCE:", 5),
        # The pasted form matches by id regardless of the casing shown.
        ("<:kekw:1>", 1),
        ("<a:DANCE:5>", 5),
        # Whitespace around the input is tolerated.
        ("  :monkas:  ", 2),
    ],
)
def test_resolve_guild_emoji_matches_the_short_name_case_insensitively(
    raw, expected_id
):
    resolved = _resolve_guild_emoji(_emoji_guild(), raw)
    assert resolved is not None, f"{raw!r} should resolve"
    assert resolved.id == expected_id


def test_resolve_guild_emoji_prefers_the_exact_casing_when_both_exist():
    """A server holding both spellings still resolves each one precisely."""
    guild = _FakeGuild(_FakeEmoji("pepe", 10), _FakeEmoji("PEPE", 11))
    assert _resolve_guild_emoji(guild, ":pepe:").id == 10
    assert _resolve_guild_emoji(guild, ":PEPE:").id == 11


@pytest.mark.parametrize("reverse", [False, True])
def test_resolve_guild_emoji_ambiguous_casing_picks_the_oldest_stably(reverse):
    """Matching no casing exactly resolves to the lowest id, whatever the order.

    Discord doesn't enforce emoji-name uniqueness and ``guild.emojis`` arrives
    in gateway-payload order, so an unordered pick would hand the member a
    different icon after a restart or a re-upload.
    """
    emojis = [_FakeEmoji("PEPE", 11), _FakeEmoji("Pepe", 10)]
    if reverse:
        emojis.reverse()
    assert _resolve_guild_emoji(_FakeGuild(*emojis), ":PePe:").id == 10


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ":::",
        ":nothere:",
        "🎉",  # unicode emoji — role icons only take this server's customs
        "<:fromelsewhere:999>",  # another server's custom emoji, by id
    ],
)
def test_resolve_guild_emoji_rejects_what_isnt_this_servers_emoji(raw):
    assert _resolve_guild_emoji(_emoji_guild(), raw) is None


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
            return_value=(EconSettings(enabled=True), {"role_name": True}),
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
            return_value=(EconSettings(enabled=True), {"role_name": True}),
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


# ── Community Bounty (hub panel; /bounty was deleted 2026-07-29) ─────────────


@pytest.mark.asyncio
async def test_bounty_post_posts_card_and_receipt(ctx, db):
    # Escrow/state are the bounty service's job (test_create_escrows_opener_
    # stake) — this covers the cog wiring: the board card posts to the bounty
    # channel and the opener gets a receipt.
    _enable(db, bounty_channel_id=5555, bounty_min_stake=10)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 200, "grant", actor_id=1)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.post_bounty_card", new=AsyncMock()
    ) as card:
        await cog.do_bounty_post(interaction, "Draw the mascot", "as a knight", "50")

    card.assert_awaited_once()
    receipt = interaction.followup.send.await_args.args[0]
    assert "Bounty posted" in receipt


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


@pytest.mark.asyncio
async def test_post_bounty_panel_ignores_the_channel_it_is_handed(ctx, db):
    """The hub's ids are looked up *through* bounty_channel_id, so a hub posted
    anywhere else would be adopted by the restick as if it were on the board.

    That used to be enforced by refusing every other channel, which left the
    dashboard drawing a picker whose only valid answer was the setting sitting
    above it (2026-08-29 audit). The panel owns its destination now, so a
    channel arriving from anywhere — a stale page, a hand-rolled request — is
    ignored rather than obeyed or complained about.
    """
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    board = MagicMock(spec=discord.TextChannel)
    board.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: board if cid == 5555 else None

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ) as place:
        assert await cog.post_bounty_panel(guild, SimpleNamespace(id=6666)) == "msg"

    place.assert_awaited_once_with(guild, board)


@pytest.mark.asyncio
async def test_post_bounty_panel_refuses_a_board_channel_that_has_gone(ctx, db):
    """A board channel deleted since it was set resolves to nothing. Say so —
    place_or_refresh would otherwise be handed None."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: None

    with pytest.raises(ValueError, match="isn't a text channel"):
        await cog.post_bounty_panel(guild)


@pytest.mark.asyncio
async def test_post_bounty_panel_refuses_when_no_board_channel_is_set(ctx, db):
    """bounty_channel_id == 0 is what bounty_enabled() gates the feature on."""
    _enable(db, bounty_channel_id=0)
    cog = _make_cog(ctx)
    guild = FakeGuild(id=GUILD_ID)

    with pytest.raises(ValueError, match="No bounty board channel"):
        await cog.post_bounty_panel(guild)


@pytest.mark.asyncio
async def test_post_bounty_panel_places_it_in_the_board_channel(ctx, db):
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: channel if cid == 5555 else None

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ) as place:
        assert await cog.post_bounty_panel(guild) == "msg"

    place.assert_awaited_once_with(guild, channel)


@pytest.mark.asyncio
async def test_post_bounty_panel_refuses_a_channel_the_casino_hub_holds(ctx, db):
    """Two panels that both chase bot posts can't share a channel: one bottom
    slot, taken from each other on every trigger, so whichever lost is buried.

    Before core.sticky learned to ignore another panel's placement this
    configuration re-posted forever with nobody typing (2026-08-06 review, F1) —
    and prod guild 1476525656115515484 had bounty_channel_id ==
    casino_panel_channel_id with the hub simply not posted yet, i.e. one
    dashboard button-press away.
    """
    _enable(db, bounty_channel_id=5555)
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD_ID, "casino_panel_channel_id", "5555"),
        )
    cog = _make_cog(ctx)
    board = MagicMock(spec=discord.TextChannel)
    board.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: board if cid == 5555 else None

    with pytest.raises(ValueError, match="casino hub panel"):
        await cog.post_bounty_panel(guild)


@pytest.mark.asyncio
async def test_post_bounty_panel_allows_a_channel_a_human_only_panel_holds(ctx, db):
    """The shop panel only moves under human messages, so the two trade places
    visibly rather than looping. That is a warning's worth of bad, not a
    refusal's — and refusing here would block a working setup."""
    _enable(db, bounty_channel_id=5555, shop_channel_id=5555)
    cog = _make_cog(ctx)
    board = MagicMock(spec=discord.TextChannel)
    board.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: board if cid == 5555 else None

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ):
        assert await cog.post_bounty_panel(guild) == "msg"


# ── the listener's fast path ─────────────────────────────────────────────────


def test_panel_guilds_lists_only_enabled_guilds_with_a_channel(ctx, db):
    """set_known_guilds is what keeps the on_message listener a set lookup. The
    economy cog was the only migrated cog that never published it, so all five
    of its panels paid a cached id read per message in every guild, forever
    (2026-08-06 review, F6)."""
    _enable(db, guide_channel_id=11, bounty_channel_id=44)
    cog = _make_cog(ctx)

    by_kind = cog._panel_guilds()

    assert by_kind["panel"] == {GUILD_ID}
    assert by_kind["bounty"] == {GUILD_ID}
    assert by_kind["shop"] == set()  # no channel set
    # The retired leaderboard pair is not a panel kind any more, so a guild
    # cannot land on the fast path through it.
    assert "leaderboard" not in by_kind


def test_panel_guilds_excludes_a_guild_with_the_economy_off(ctx, db):
    """A disabled economy makes every panel read (0, 0), so the guild has no
    business on the fast path either."""
    with open_db(db) as conn:
        save_econ_settings(
            conn, GUILD_ID, {"enabled": False, "guide_channel_id": 11}
        )
    cog = _make_cog(ctx)

    assert cog._panel_guilds()["panel"] == set()


@pytest.mark.asyncio
async def test_publishing_panel_guilds_leaves_the_auction_card_unpublished(ctx, db):
    """The auction card is the one panel that comes and goes. It is posted
    directly rather than through place() — so nothing calls _remember to add its
    guild — and auction_views calls forget() right after posting, which would
    *discard* the guild from a published set and leave the card un-sticky. Its
    TTL cache is the gate."""
    _enable(db, guide_channel_id=11)
    cog = _make_cog(ctx)

    await cog._publish_panel_guilds()

    assert cog.economy_panel._known == {GUILD_ID}
    assert cog.auction_panel._known is None


def test_bounty_panel_ids_are_dark_until_the_board_is_configured(ctx, db):
    """(0, 0) reads as "unposted", so the restick is a no-op — the same
    condition bounty_enabled() gates the whole feature on."""
    cog = _make_cog(ctx)
    posted = {"bounty_panel_channel_id": 5555, "bounty_panel_message_id": 77}

    _enable(db, bounty_channel_id=0, **posted)
    assert cog._bounty_panel_ids(GUILD_ID) == (0, 0)

    _enable(db, enabled=False, bounty_channel_id=5555, **posted)
    assert cog._bounty_panel_ids(GUILD_ID) == (0, 0)

    _enable(db, enabled=True, bounty_channel_id=5555, **posted)
    assert cog._bounty_panel_ids(GUILD_ID) == (5555, 77)


def test_saving_bounty_panel_ids_never_moves_the_board(ctx, db):
    """Only the message id is stored — writing the channel back would let a
    mis-posted hub silently redefine which channel the board is."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)

    cog._save_bounty_panel_ids(GUILD_ID, 9999, 4242)

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.bounty_channel_id == 5555
    assert settings.bounty_panel_message_id == 4242


@pytest.mark.asyncio
async def test_refresh_bounty_hub_is_a_no_op_before_the_hub_is_posted(ctx, db):
    """Otherwise place_or_refresh would *create* a hub in a guild that never
    set one up, the first time anybody chipped in."""
    _enable(db, bounty_channel_id=5555, bounty_panel_message_id=0)
    cog = _make_cog(ctx)

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock()
    ) as place:
        await cog.refresh_bounty_hub_panel(FakeGuild(id=GUILD_ID))

    place.assert_not_awaited()


def test_no_bounty_slash_command_survives():
    """The hub panel replaced /bounty — CLAUDE.md's one-panel-over-subcommands."""
    from bot_modules.cogs.economy_cog import EconomyCog

    names = {c.name for c in EconomyCog.__cog_app_commands__}
    assert "bounty" not in names


def test_the_bounty_hub_follows_its_own_boards_cards(ctx, db):
    """The hub is the board's only entry point, and the board's own channel is
    where the bot posts cards — so without restick_on_bot a burst of new
    bounties strands the hub above them until a human happens to speak
    (2026-07-29).

    The wiring assertion is the whole change: the burst behaviour it buys is
    covered at the logic layer in test_core_sticky.py. The auction card is the
    contrast — nothing bot-authored lands in its channel while it is live, so
    the flag would buy it nothing.
    """
    cog = _make_cog(ctx)
    assert cog.bounty_panel._restick_on_bot is True
    assert cog.auction_panel._restick_on_bot is False


def test_repointing_the_bounty_board_stops_the_old_hub_being_restuck(ctx, db):
    """Changing bounty_channel_id doesn't touch the panel ids (the dashboard
    save is a partial update). Pairing the old message with the new channel
    would edit-404, post a second hub, and fail to delete the first."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    cog._save_bounty_panel_ids(GUILD_ID, 5555, 4242)
    assert cog._bounty_panel_ids(GUILD_ID) == (5555, 4242)

    _enable(db, bounty_channel_id=6666)  # admin repoints the board

    assert cog._bounty_panel_ids(GUILD_ID) == (0, 0)


@pytest.mark.asyncio
async def test_posting_the_bounty_panel_deletes_a_hub_orphaned_by_a_repoint(ctx, db):
    """An orphaned hub's buttons are static custom_ids, so it stays live —
    Post a bounty on it would file a card into the new board channel."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    cog._save_bounty_panel_ids(GUILD_ID, 5555, 4242)
    _enable(db, bounty_channel_id=6666)

    old_message = MagicMock()
    old_message.delete = AsyncMock()
    old_channel = MagicMock(spec=discord.TextChannel)
    old_channel.get_partial_message.return_value = old_message
    new_channel = MagicMock(spec=discord.TextChannel)
    new_channel.id = 6666
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: old_channel if cid == 5555 else new_channel

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ):
        await cog.post_bounty_panel(guild)

    old_channel.get_partial_message.assert_called_once_with(4242)
    old_message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_posting_the_bounty_panel_does_not_delete_a_hub_in_the_current_board(ctx, db):
    """Re-posting into the same channel is a refresh, not a move — deleting
    here would destroy the panel the sticky is about to edit in place."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    cog._save_bounty_panel_ids(GUILD_ID, 5555, 4242)

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: channel

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ):
        await cog.post_bounty_panel(guild)

    channel.get_partial_message.assert_not_called()
    with open_db(db) as conn:
        assert load_econ_settings(conn, GUILD_ID).bounty_panel_message_id == 4242


@pytest.mark.asyncio
async def test_qotd_ping_cannot_smuggle_an_everyone_mention(ctx, db):
    """AllowedMentions' unset fields default to ALLOW, so the ping's
    allow-list has to pin everyone/users off explicitly — otherwise a question
    containing @everyone would ping the whole server."""
    _enable(db, qotd_ping_role_id=4242)
    cog = _make_cog(ctx)
    interaction, channel = _qotd_interaction(_member(admin=True))
    interaction.guild.roles[4242] = FakeRole(id=4242, name="QOTD")
    await _qotd(cog, interaction, "@everyone what's for dinner?")

    allowed = channel.send.await_args.kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.replied_user is False


# ── custom shop items (glue only — the money is tested in the service) ──


def _store_item(db, **over):
    from bot_modules.services.economy_shop_items_service import create_item

    fields = {"name": "Shoutout", "price": 100}
    fields.update(over)
    with open_db(db) as conn:
        return create_item(conn, GUILD_ID, **fields)


@pytest.mark.asyncio
async def test_buying_a_role_item_grants_the_role(tmp_path):
    """The one thing the cog owns: telling Discord about a completed sale."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, kind="role", role_id=777, billing="once")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)
    guild = interaction.guild
    role = MagicMock(spec=discord.Role)
    guild.get_role = MagicMock(return_value=role)
    guild.get_member = MagicMock(return_value=actor)
    actor.roles = []
    actor.add_roles = AsyncMock()

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, guild, item)

    actor.add_roles.assert_awaited_once()
    assert "Bought" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_a_failed_role_grant_still_keeps_the_purchase(tmp_path):
    """The member paid and the order exists — refusing now would contradict
    a debit that has already been taken, so it reads as staff work instead."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, kind="role", role_id=777, billing="once")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)
    guild = interaction.guild
    guild.get_role = MagicMock(return_value=None)  # role deleted since setup
    guild.get_member = MagicMock(return_value=actor)

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, guild, item)

    text = interaction.response.send_message.await_args.args[0]
    assert "couldn't hand you the role" in text
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 400


@pytest.mark.asyncio
async def test_buying_a_manual_item_promises_no_time(tmp_path):
    """A human has to do it; inventing an ETA is how a member decides the bot
    is broken an hour later."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, interaction.guild, item)

    text = interaction.response.send_message.await_args.args[0]
    assert "staff" in text.lower()
    assert "back" in text  # the refund promise


@pytest.mark.asyncio
async def test_an_unaffordable_item_is_refused_without_charging(tmp_path):
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, price=100)

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, interaction.guild, item)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 0
        rows = conn.execute("SELECT COUNT(*) AS n FROM econ_shop_purchases").fetchone()
    assert rows["n"] == 0


@pytest.mark.asyncio
async def test_buying_a_staff_item_repaints_the_todo_board(tmp_path):
    """The order lands on the mods' board, so the board has to be told.

    Every other path that adds a todo repaints (/todo, the board's own Add
    button, the dashboard). This one didn't, and the 60s loop is no backstop —
    it only repaints guilds where a recurring chore spawned or was written off.
    A bought order therefore stayed invisible in Discord until somebody
    happened to chat in the board's channel.
    """
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    todo_cog = MagicMock()
    todo_cog.refresh_board = AsyncMock()
    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    cog.bot.get_cog = MagicMock(
        side_effect=lambda name: todo_cog if name == "TodoCog" else None
    )
    interaction = _interaction(_member(member_id=500))

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, interaction.guild, item)

    todo_cog.refresh_board.assert_awaited_once_with(GUILD_ID)


@pytest.mark.asyncio
async def test_buying_a_role_item_does_not_touch_the_todo_board(tmp_path):
    """Nothing lands on the board, so nothing needs repainting."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, kind="role", role_id=777, billing="once")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    todo_cog = MagicMock()
    todo_cog.refresh_board = AsyncMock()
    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    cog.bot.get_cog = MagicMock(
        side_effect=lambda name: todo_cog if name == "TodoCog" else None
    )
    actor = _member(member_id=500)
    interaction = _interaction(actor)
    interaction.guild.get_role = MagicMock(return_value=MagicMock(spec=discord.Role))
    interaction.guild.get_member = MagicMock(return_value=actor)
    actor.roles = []
    actor.add_roles = AsyncMock()

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, interaction.guild, item)

    todo_cog.refresh_board.assert_not_awaited()


# ── the shop as one paged book ─────────────────────────────────────────────


def _shop_view(interaction):
    return interaction.response.send_message.await_args.kwargs["view"]


def _shop_embed(interaction):
    return interaction.response.send_message.await_args.kwargs["embed"]


@pytest.mark.asyncio
async def test_the_shop_opens_on_the_store_where_a_guild_stocks_one(ctx, db):
    """Billy's ask, end to end: one panel button, store first, zero extra taps.

    The panel button and /bank shop land on the same page — the store — so the
    server's own goods are what a member sees on arrival rather than what they
    click through to.
    """
    from bot_modules.cogs.economy_cog import _StoreView

    _enable(db)
    _store_item(db, name="Shoutout")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _shop(cog, interaction)

    assert _shop_embed(interaction).title == "🎁 Specials"
    assert isinstance(_shop_view(interaction), _StoreView)


def _nav(view):
    """The shop's navigation row: (label, disabled) for each of its three."""
    return [
        (b.label, b.disabled)
        for b in view.children
        if isinstance(b, discord.ui.Button) and getattr(b, "row", None) == 4
    ]


@pytest.mark.asyncio
async def test_a_guild_with_no_store_never_sees_a_specials_page(ctx, db):
    """TGM today: no custom items, so that section simply is not in its book.

    An empty section gets no page rather than a page saying it is empty — the
    shop's shape follows what the guild actually sells.
    """
    from bot_modules.cogs.economy_cog import _ShopView

    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, interaction)

    view = _shop_view(interaction)
    assert isinstance(view, _ShopView)
    # Opens on the first section it does have, not on a blank shelf.
    assert _shop_embed(interaction).title == "🎨 Role cosmetics"
    assert "🎁 Specials" not in [label for label, _ in _nav(view)]


@pytest.mark.asyncio
async def test_a_shop_with_one_section_left_shows_no_arrows(ctx, db):
    """Two dead arrows around the only page there is would be furniture.

    The floor case: every perk switched off and no raffle, so cosmetics is all
    that remains.
    """
    from bot_modules.services.economy_service import SHOP_TOGGLE_PERKS

    _enable(db, raffle_enabled=False,
            **{f"shop_{p}_enabled": False for p in SHOP_TOGGLE_PERKS})
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, interaction)

    assert not _nav(_shop_view(interaction))


@pytest.mark.asyncio
async def test_the_arrows_step_the_sections_and_say_where_you_are(ctx, db):
    """Billy's shape: ◀️, the page you are on, ▶️.

    The caption is a disabled button because Discord has no label component —
    and it is what bare arrows lacked, which moved you without ever saying
    where you had been moved to.
    """
    _enable(db)
    for n in range(1, 21):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, interaction)
        assert _nav(_shop_view(interaction)) == [
            ("◀️", False), ("🎁 Specials · 1–10", True), ("▶️", False),
        ]

        # ▶️ steps to the next store section: new caption, new embed rows.
        step = _interaction(_member(member_id=500))
        nxt = [b for b in _shop_view(interaction).children
               if isinstance(b, discord.ui.Button) and b.label == "▶️"][0]
        await nxt.callback(step)

    second = step.response.edit_message.await_args.kwargs
    assert _nav(second["view"])[1] == ("🎁 Specials · 11–20", True)
    assert "Item 11" in second["embed"].description
    assert "Item 01" not in second["embed"].description


@pytest.mark.asyncio
async def test_stepping_changes_what_the_dropdown_will_sell_you(ctx, db):
    """The section drives the buy picker, not just the embed text.

    Billy's note on the sketch: the arrows change what shows *and* what is
    selectable. A picker still offering page 1's items from page 2 would sell
    the wrong thing.
    """
    _enable(db)
    for n in range(1, 21):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)

    first = _interaction(_member(member_id=500))
    await _shop(cog, first)
    opts = next(
        c for c in _shop_view(first).children if isinstance(c, discord.ui.Select)
    ).options
    assert [o.label for o in opts] == [f"Item {n:02d}" for n in range(1, 11)]

    second = _interaction(_member(member_id=500))
    await cog.turn_shop_page(second, 1)
    opts = next(
        c for c in second.response.edit_message.await_args.kwargs["view"].children
        if isinstance(c, discord.ui.Select)
    ).options
    assert [o.label for o in opts] == [f"Item {n:02d}" for n in range(11, 21)]


@pytest.mark.asyncio
async def test_the_arrows_wrap_rather_than_dead_ending(ctx, db):
    """From the last page ▶️ comes round to the first.

    A greyed-out arrow at the end reads as a broken button more often than as
    "you are at the end".
    """
    _enable(db)
    for n in range(1, 21):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        last = _interaction(_member(member_id=500))
        await cog.turn_shop_page(last, 4)          # 🎲 Game features, the last
        view = last.response.edit_message.await_args.kwargs["view"]
        assert _nav(view)[1] == ("🎲 Game features", True)

        wrapped = _interaction(_member(member_id=500))
        nxt = [b for b in view.children
               if isinstance(b, discord.ui.Button) and b.label == "▶️"][0]
        await nxt.callback(wrapped)

    assert _nav(wrapped.response.edit_message.await_args.kwargs["view"])[1] == (
        "🎁 Specials · 1–10", True
    )


@pytest.mark.asyncio
async def test_a_big_store_keeps_the_same_three_controls(ctx, db):
    """Forty-five items is six pages and still ◀️ · caption · ▶️.

    This is why there is no overflow shape: a tab row ran out at five pages,
    and a select spent a whole row saying the same thing.
    """
    _enable(db)
    for n in range(1, 46):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _shop(cog, interaction)

    view = _shop_view(interaction)
    assert _nav(view) == [("◀️", False), ("🎁 Specials · 1–10", True), ("▶️", False)]
    # ◀️ from the first page lands on the perks, the last of six.
    assert len([c for c in view.children if isinstance(c, discord.ui.Select)]) == 1


@pytest.mark.asyncio
async def test_the_perk_page_does_not_repeat_the_store(ctx, db):
    """The store has its own pages now — a preview here is the same list twice.

    It also restores the plain "Weekly rentals" header, which is true again
    once the page holds nothing but weekly perks.
    """
    _enable(db)
    _store_item(db, name="Shoutout")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, interaction)
        turn = _interaction(_member(member_id=500))
        await cog.turn_shop_page(turn, 1)

    embed = turn.response.edit_message.await_args.kwargs["embed"]
    assert "Server Store" not in [f.name for f in embed.fields]
    assert embed.description.startswith("Weekly rentals")


@pytest.mark.asyncio
async def test_the_panel_keeps_its_single_button(ctx, db):
    """The second Server Store button was built and taken back out.

    The shop it opens now starts on the store, so the shortcut had nothing
    left to shorten — and a panel is a launcher, not a button row.
    """
    from bot_modules.cogs.economy_cog import ShopPanelView, _StoreView

    _enable(db)
    _store_item(db, name="Shoutout")
    cog = _make_cog(ctx)

    assert [b.custom_id for b in ShopPanelView().children] == ["econ_shop_open"]

    interaction = _panel_button_interaction(ctx, cog)
    await ShopPanelView()._open.callback(interaction)
    assert isinstance(_shop_view(interaction), _StoreView)


@pytest.mark.asyncio
async def test_bank_store_is_the_shop_by_a_shorter_name(ctx, db):
    """Same view, same page — a door, not a second shop."""
    from bot_modules.cogs.economy_cog import _StoreView

    _enable(db)
    _store_item(db, name="Shoutout")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.bank_store.callback(cog, interaction)

    assert isinstance(_shop_view(interaction), _StoreView)
    assert _shop_embed(interaction).title == "🎁 Specials"


@pytest.mark.asyncio
async def test_bank_store_refuses_by_name_with_nothing_stocked(ctx, db):
    """Serving the perk ladder here would read as the command being wrong."""
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.bank_store.callback(cog, interaction)

    text = interaction.response.send_message.await_args.args[0]
    assert "hasn't put anything in the store yet" in text


@pytest.mark.asyncio
async def test_bank_store_refuses_when_the_economy_is_off(ctx, db):
    """Same gate as every other member-facing economy surface."""
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.bank_store.callback(cog, interaction)

    assert "embed" not in interaction.response.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_a_page_turn_past_the_end_of_a_shrunken_shop_clamps(ctx, db):
    """An admin can withdraw the last item while a member sits on page 2.

    Trusting the number would hand them an empty store page; clamping lands
    them on the perk ladder, which is the only page left.
    """
    from bot_modules.cogs.economy_cog import _ShopView

    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.turn_shop_page(interaction, 7)

    view = interaction.response.edit_message.await_args.kwargs["view"]
    assert isinstance(view, _ShopView)


@pytest.mark.asyncio
async def test_navigation_is_always_its_own_bottom_row(ctx, db):
    """Navigation sits in the same place on every page, under everything else.

    Asserted on the rendered component rows rather than on ``row=``, because
    what matters is what Discord draws: left to auto-pack, the old arrows
    slotted in beside Shield and Cancel & Refund, putting "Next" where a
    member is aiming for a refund.
    """
    _enable(db)
    for n in range(1, 21):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)

    def _rows(view):
        return [
            [c.get("label") or c.get("placeholder") for c in row["components"]]
            for row in view.to_components()
        ]

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        # A store page: the buy picker's row, then navigation on its own.
        store = _interaction(_member(member_id=500))
        await _shop(cog, store)
        srows = _rows(_shop_view(store))
        assert srows == [
            ["Pick something to buy…"], ["◀️", "🎁 Specials · 1–10", "▶️"],
        ]

        # The perk page, where eight buttons would otherwise leave a gap the
        # navigation falls into.
        perks = _interaction(_member(member_id=500))
        await cog.turn_shop_page(perks, 4)

    rows = _rows(perks.response.edit_message.await_args.kwargs["view"])
    assert rows[-1] == ["◀️", "🎲 Game features", "▶️"]   # its own last row
    assert len(rows) > 1                           # and not the only row
    assert all("◀️" not in r for r in rows[:-1])


@pytest.mark.asyncio
async def test_each_section_carries_only_its_own_controls(ctx, db):
    """A page must not hold buttons for rows its embed doesn't show.

    Otherwise a member taps 🛡️ Shield on the cosmetics page and has to work
    out afterwards what they just bought.
    """
    from bot_modules.economy.shop import (
        SECTION_COSMETICS, SECTION_GAMES, SECTION_SERVER,
    )

    _enable(db, shop_voice_style_enabled=True)
    cog = _make_cog(ctx)

    def _ids(section):
        view = _ShopView(
            cog, _settings(db), _guild_roles(), 500, set(), set(),
            has_palette=True, shield_price=250, section=section,
        )
        return {i for i in _offers(view) if i.startswith("econ_shop_")}

    cosmetics, server, games = map(
        _ids, (SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES)
    )
    assert "econ_shop_rent:role_color" in cosmetics
    assert "econ_shop_shield" not in cosmetics
    assert "econ_shop_rent:voice_style" not in cosmetics

    assert server == {
        "econ_shop_rent:voice_style", "econ_shop_sponsor", "econ_shop_refund",
    }
    assert "econ_shop_shield" in games
    assert "econ_shop_rent:role_color" not in games


@pytest.mark.asyncio
async def test_cancel_and_refund_rides_every_section(ctx, db):
    """It ends *any* rental, so filing it under one section would hide it.

    A member whose only rental is the voice lease must not have to guess that
    the refund button lives on the cosmetics page.
    """
    from bot_modules.economy.shop import (
        SECTION_COSMETICS, SECTION_GAMES, SECTION_SERVER,
    )

    _enable(db, shop_voice_style_enabled=True)
    cog = _make_cog(ctx)

    for section in (SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES):
        view = _ShopView(
            cog, _settings(db), _guild_roles(), 500, set(), set(),
            has_palette=True, shield_price=250, section=section,
        )
        assert "econ_shop_refund" in _offers(view), section


@pytest.mark.asyncio
async def test_a_section_with_several_products_offers_a_picker(ctx, db):
    """Billy's ask: one dropdown scoped to the section, not a row of buttons.

    Cancel & Refund stays a button — it is not a product, and it is the one
    destructive control here, so burying it in a list of things to buy is how
    somebody cancels a rental they meant to keep.
    """
    from bot_modules.cogs.economy_cog import _ActionSelect

    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, interaction)

    view = _shop_view(interaction)
    picker = next(c for c in view.children if isinstance(c, _ActionSelect))
    assert picker.placeholder == "Rent or customize a perk…"
    assert {o.value for o in picker.options} >= {
        "econ_shop_rent:role_color", "econ_shop_rent:role_name",
    }
    # Every option says what the thing is, not just what it's called.
    assert all(o.description for o in picker.options)


@pytest.mark.asyncio
async def test_even_a_lone_product_gets_a_picker(ctx, db):
    """Every section wears the same control, whatever its guild sells.

    A one-option dropdown costs a tap over a button, but which sections hold a
    single product changes with a dashboard toggle — so letting the shape
    follow the count would move the furniture under a member for reasons they
    cannot see.
    """
    from bot_modules.cogs.economy_cog import _ActionSelect
    from bot_modules.economy.shop import SECTION_SERVER

    _enable(db, shop_voice_style_enabled=True, price_qotd_sponsor=0)
    view = _ShopView(
        cog=_make_cog(ctx), settings=_settings(db), guild=_guild_roles(),
        user_id=500, gated=set(), owned=set(), section=SECTION_SERVER,
    )
    picker = next(c for c in view.children if isinstance(c, _ActionSelect))
    assert [o.value for o in picker.options] == ["econ_shop_rent:voice_style"]


@pytest.mark.asyncio
async def test_an_unavailable_row_is_listed_and_explains_itself(ctx, db):
    """Discord can't grey out one option, so the refusal happens on choosing.

    Dropping the row instead would make the picker disagree with the table
    right above it, which reads as the control being broken rather than the
    product being unavailable.
    """
    from bot_modules.cogs.economy_cog import _ActionSelect

    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    async def _gate(bot, guild_id, perk):
        return perk != "role_gradient"

    with patch(
        "bot_modules.cogs.economy_cog.feature_gate_ok",
        new=AsyncMock(side_effect=_gate),
    ):
        await _shop(cog, interaction)

    picker = next(
        c for c in _shop_view(interaction).children if isinstance(c, _ActionSelect)
    )
    # Still listed — the table above still shows the row.
    assert "econ_shop_rent:role_gradient" in {o.value for o in picker.options}

    chose = _interaction(_member(member_id=500))
    picker._values = ["econ_shop_rent:role_gradient"]
    await picker.callback(chose)
    assert "server feature" in chose.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_the_channel_panel_carries_no_prices(ctx, db):
    """It is a poster, not a catalogue.

    The listing one tap away is fuller *and* correct for the viewer — their
    balance, their ✅ marks, their gated rows — so duplicating a priced table
    here only gave it something to go stale against.
    """
    _enable(db, price_role_name=35, price_streak_shield=30)
    cog = _make_cog(ctx)

    content = await cog._build_shop_panel(_guild_roles())

    embed = content.embed
    blob = embed.description + " ".join(f.value for f in embed.fields)
    assert "35" not in blob and "30" not in blob
    # It names its shelves instead, from the same list the shop itself walks.
    assert "🎨 Role cosmetics" in blob
    assert [b.custom_id for b in content.view.children] == ["econ_shop_open"]


@pytest.mark.asyncio
async def test_the_consumables_are_bought_from_the_shop_not_a_command(ctx, db):
    """A product reachable only by knowing a command's name is one most
    members never find, so the themed day, the sponsored question and the
    paid pin moved into 🏠 Server features — and their commands went with
    them, rather than leaving two surfaces to drift.
    """
    from bot_modules.cogs.economy_cog import EconomyCog
    from bot_modules.economy.shop import SECTION_SERVER

    assert not hasattr(EconomyCog, "bank_theme")
    assert not hasattr(EconomyCog, "bank_sponsor")
    assert not hasattr(EconomyCog, "bank_pin")

    _enable(
        db, shop_voice_style_enabled=True, flash_theme_enabled=True,
        theme_channel_id=42, price_flash_theme=300,
        price_qotd_sponsor=40, price_pin_of_day=25, pin_channel_id=43,
    )
    view = _ShopView(
        cog=_make_cog(ctx), settings=_settings(db), guild=_guild_roles(),
        user_id=500, gated=set(), owned=set(), section=SECTION_SERVER,
    )
    assert set(_offers(view)) >= {
        "econ_shop_rent:voice_style", "econ_shop_theme",
        "econ_shop_sponsor", "econ_shop_pin",
    }


@pytest.mark.asyncio
async def test_choosing_a_consumable_opens_its_modal(ctx, db):
    """Each one still needs the member's words, so the picker opens the box.

    A modal must be an interaction's first response, which is why these go
    straight to it — the control only exists when the enable check passed, and
    the submit handler re-checks before taking money.
    """
    from bot_modules.cogs.economy_cog import _SponsorSubmitModal
    from bot_modules.economy.shop import SECTION_SERVER

    _enable(db, price_qotd_sponsor=40)
    view = _ShopView(
        cog=_make_cog(ctx), settings=_settings(db), guild=_guild_roles(),
        user_id=500, gated=set(), owned=set(), section=SECTION_SERVER,
    )
    interaction = _interaction(_member(member_id=500))
    await _entry(view, "econ_shop_sponsor").callback(interaction)

    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, _SponsorSubmitModal)


@pytest.mark.asyncio
async def test_a_sponsored_question_from_the_shop_still_escrows(ctx, db):
    """The modal path lands in the same handler the command used to call."""
    _enable(db, price_qotd_sponsor=40)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    # The request goes to the mods' todo board, never to the bank channel —
    # that channel is a member-facing explainer in the main guild, and the
    # card names the member and quotes what they wrote.
    with patch(
        "bot_modules.cogs.economy_cog.refresh_approvals_board", new=AsyncMock()
    ) as board:
        await cog.do_sponsor_submit(interaction, "What's your comfort meal?")

    board.assert_awaited_once()
    assert "review" in interaction.followup.send.await_args.args[0]
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 460
