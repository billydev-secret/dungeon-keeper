"""Tests for the todo board's Approvals button (economy/approval_views.py).

One button over three paid queues — a themed day, a sponsored question, a pin.
The bug these exist for: each of those used to post its Approve/Decline card
into the economy's ``bank_channel_id``, which in the main guild is a
member-facing explainer channel. An unreviewed request naming the member and
quoting what they wrote was published to the whole server. So the properties
worth asserting are that nothing reaches a channel, that the review surface is
the mods' board, and that resolving from it still moves the money exactly
once.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.approval_views import (
    ApprovalPickSelect,
    open_approvals_picker,
    option_text,
)
from bot_modules.economy.pin_views import PinReviewView
from bot_modules.economy.pin_views import _handle_resolution as resolve_pin
from bot_modules.economy.sponsor_views import SponsorReviewView
from bot_modules.economy.sponsor_views import (
    _handle_resolution as resolve_sponsor,
)
from bot_modules.economy.theme_views import ThemeReviewView
from bot_modules.economy.theme_views import _handle_resolution as resolve_theme
from bot_modules.services.economy_pin_service import submit_pin
from bot_modules.services.economy_qotd_sponsor_service import submit_sponsor
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
    load_econ_settings,
    save_econ_settings,
)
from bot_modules.services.economy_theme_service import get_submission, submit_theme
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
BANK_CHANNEL = 424242
REQUESTER = 500
MANAGER_ROLE = 7007


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db_path=db, open_db=lambda: open_db(db))


@pytest.fixture(autouse=True)
def _patch_accent():
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        yield


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {
        "enabled": True,
        "bank_channel_id": BANK_CHANNEL,
        "flash_theme_enabled": True,
        "price_flash_theme": 300,
        "theme_channel_id": 6666,
        "price_qotd_sponsor": 40,
        "price_pin_of_day": 150,
        "pin_channel_id": 6668,
    }
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _settings(db) -> EconSettings:
    with open_db(db) as conn:
        return load_econ_settings(conn, GUILD_ID)


def _member(*, admin=False, role_ids=(), member_id=REQUESTER, name="Alex") -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = False
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.guild_permissions = MagicMock(administrator=admin)
    m.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return m


def _fund(db, user_id=REQUESTER, amount=5000) -> None:
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, user_id, amount, "grant", actor_id=1)


def _theme(db, user_id=REQUESTER, title="Cursed Cooking") -> int:
    _fund(db, user_id)
    with open_db(db) as conn:
        return submit_theme(
            conn, _settings(db), GUILD_ID, user_id, title, "The idea"
        ).submission_id


def _sponsor(db, user_id=REQUESTER, question="What is your comfort food?") -> int:
    _fund(db, user_id)
    with open_db(db) as conn:
        return submit_sponsor(
            conn, _settings(db), GUILD_ID, user_id, question
        ).submission_id


def _pin(db, user_id=REQUESTER, message="Raid at eight") -> int:
    _fund(db, user_id)
    with open_db(db) as conn:
        return submit_pin(
            conn, _settings(db), GUILD_ID, user_id, message
        ).submission_id


def _age(db, table, row_id, seconds):
    with open_db(db) as conn:
        conn.execute(
            f"UPDATE {table} SET created_at = created_at - ? WHERE id = ?",
            (seconds, row_id),
        )


def _bot(ctx) -> MagicMock:
    bot = MagicMock()
    bot.ctx = ctx
    # The todo board is the review surface now, so every path that files or
    # resolves a request repaints it. Exposed as ``bot.todo_refresh`` so a
    # test can assert the repaint without reaching through get_cog.
    bot.todo_refresh = AsyncMock(return_value=True)
    bot.get_cog = MagicMock(
        return_value=SimpleNamespace(refresh_board=bot.todo_refresh)
    )
    return bot


def _picker_interaction(bot, *, user, members=(), message=None) -> MagicMock:
    guild = FakeGuild(id=GUILD_ID, members={m.id: m for m in members})
    i = fake_interaction(guild=guild)
    i.client = bot
    i.user = user
    i.message = message
    return i


def _ephemeral_message() -> MagicMock:
    """The picker's detail view: a real message, but an ephemeral one."""
    msg = MagicMock()
    msg.flags = MagicMock(ephemeral=True)
    msg.edit = AsyncMock()
    return msg


def _legacy_card() -> MagicMock:
    """A bank-channel card from before the move. Nothing posts these any more,
    but the ones already out there stay clickable."""
    card = MagicMock()
    card.flags = MagicMock(ephemeral=False)
    card.edit = AsyncMock()
    return card


# ── the select's copy ──────────────────────────────────────────────────


def test_an_option_names_the_member_and_the_queue(db):
    _enable(db)
    label, desc = option_text(
        {
            "kind": "theme",
            "id": 1,
            "user_id": REQUESTER,
            "price": 300,
            "summary": "Cursed Cooking",
            "requester_name": "Alex",
        },
        _settings(db),
    )
    assert label == "Alex — 🎨 Theme"
    assert "300 Coins" in desc
    assert "Cursed Cooking" in desc


def test_an_option_stays_inside_discords_hundred_character_caps(db):
    _enable(db)
    label, desc = option_text(
        {
            "kind": "pin",
            "id": 1,
            "user_id": REQUESTER,
            "price": 150,
            "summary": "x" * 400,
            "requester_name": "y" * 400,
        },
        _settings(db),
    )
    assert len(label) <= 100
    assert len(desc) <= 100


def test_a_requester_who_left_is_never_a_raw_id(db):
    _enable(db)
    label, _ = option_text(
        {"kind": "pin", "id": 1, "user_id": REQUESTER, "price": 0,
         "summary": "Raid", "requester_name": ""},
        _settings(db),
    )
    assert label.startswith("someone — ")
    assert str(REQUESTER) not in label


# ── the picker ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_picker_offers_all_three_queues_oldest_first(ctx, db):
    _enable(db)
    theme_id = _theme(db, user_id=REQUESTER)
    sponsor_id = _sponsor(db, user_id=501)
    pin_id = _pin(db, user_id=502)
    _age(db, "econ_pin_submissions", pin_id, 10_000)
    _age(db, "econ_qotd_submissions", sponsor_id, 5_000)

    requester = _member(member_id=REQUESTER, name="Alex")
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999), members=[requester]
    )
    await open_approvals_picker(interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    values = [o.value for o in view.children[0].options]
    assert values == [f"pin:{pin_id}", f"sponsor:{sponsor_id}", f"theme:{theme_id}"]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_the_picker_refuses_a_non_manager(ctx, db):
    """Every decision behind this button moves currency, so it is the
    economy's manager gate — not the board's own moderator check."""
    _enable(db, manager_role_id=MANAGER_ROLE)
    _theme(db)
    interaction = _picker_interaction(_bot(ctx), user=_member(role_ids=(1,)))

    await open_approvals_picker(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "permission" in msg.lower()
    assert "view" not in interaction.response.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_the_picker_says_so_when_nothing_is_waiting(ctx, db):
    _enable(db)
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999)
    )

    await open_approvals_picker(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "no paid requests" in msg.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "make", "view_cls", "title", "quote"),
    [
        pytest.param(
            "theme", _theme, ThemeReviewView, "📋 Theme Requested", "The idea",
            id="theme",
        ),
        pytest.param(
            "sponsor", _sponsor, SponsorReviewView,
            "📋 Sponsored Question Submitted", "comfort food", id="sponsor",
        ),
        pytest.param(
            "pin", _pin, PinReviewView, "📋 Pin Requested", "Raid at eight",
            id="pin",
        ),
    ],
)
async def test_picking_a_request_opens_the_products_own_card(
    ctx, db, kind, make, view_cls, title, quote
):
    """The same embed builder the bank-channel card used, with that product's
    own buttons — nothing a moderator reads changed, only where they read it."""
    _enable(db)
    submission_id = make(db)

    select = ApprovalPickSelect(
        [{"kind": kind, "id": submission_id, "user_id": REQUESTER, "price": 300,
          "summary": "whatever", "requester_name": "Alex"}],
        _settings(db),
    )
    select._values = [f"{kind}:{submission_id}"]  # type: ignore[attr-defined]
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999)
    )
    await select.callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert isinstance(kwargs["view"], view_cls)
    embed = kwargs["embed"]
    assert embed.title == title
    assert any(quote in str(f.value) for f in embed.fields)


@pytest.mark.asyncio
async def test_picking_an_already_resolved_request_offers_no_buttons(ctx, db):
    """Someone else got there first — show the outcome, not a live decision."""
    _enable(db)
    submission_id = _theme(db)
    with open_db(db) as conn:
        from bot_modules.services.economy_theme_service import deny

        deny(conn, submission_id, resolver_id=111, deny_reason="too close")

    select = ApprovalPickSelect(
        [{"kind": "theme", "id": submission_id, "user_id": REQUESTER, "price": 300,
          "summary": "Cursed Cooking", "requester_name": "Alex"}],
        _settings(db),
    )
    select._values = [f"theme:{submission_id}"]  # type: ignore[attr-defined]
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999)
    )
    await select.callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["view"] is None
    assert kwargs["embed"].title == "❌ Theme Declined"


@pytest.mark.asyncio
async def test_picking_a_request_that_has_since_vanished_says_so(ctx, db):
    _enable(db)
    select = ApprovalPickSelect([], _settings(db))
    select._values = ["theme:99999"]  # type: ignore[attr-defined]
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999)
    )
    await select.callback(interaction)

    interaction.response.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stale_queue_key_is_a_shrug_not_a_crash(ctx, db):
    """The value rides on a long-lived ephemeral message; a key this build no
    longer knows is 'that's gone now'."""
    _enable(db)
    select = ApprovalPickSelect([], _settings(db))
    select._values = ["emoji:1"]  # type: ignore[attr-defined]
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999)
    )
    await select.callback(interaction)

    interaction.response.edit_message.assert_not_awaited()


# ── resolving from the board ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make", "resolve", "declined_title"),
    [
        pytest.param(_theme, resolve_theme, "❌ Theme Declined", id="theme"),
        pytest.param(
            _sponsor, resolve_sponsor, "❌ Sponsored Question Declined",
            id="sponsor",
        ),
        pytest.param(_pin, resolve_pin, "❌ Pin Declined", id="pin"),
    ],
)
async def test_declining_from_the_board_repaints_the_ephemeral_not_a_channel(
    ctx, db, make, resolve, declined_title
):
    """The regression. The detail view is a real message and reaches the
    resolver like a card would, but an ephemeral message cannot be edited
    through the channel-message endpoint — only through its own interaction."""
    _enable(db)
    submission_id = make(db)
    detail = _ephemeral_message()
    bot = _bot(ctx)
    interaction = _picker_interaction(
        bot, user=_member(admin=True, member_id=999), message=detail
    )

    await resolve(
        interaction, submission_id, approve=False, deny_reason="too close"
    )

    detail.edit.assert_not_awaited()
    embed = interaction.edit_original_response.await_args.kwargs["embed"]
    assert embed.title == declined_title
    bot.todo_refresh.assert_awaited_with(GUILD_ID)


@pytest.mark.asyncio
async def test_declining_from_the_board_moves_the_row_and_refunds(ctx, db):
    _enable(db)
    submission_id = _theme(db)
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999),
        message=_ephemeral_message(),
    )

    await resolve_theme(
        interaction, submission_id, approve=False, deny_reason="too close"
    )

    with open_db(db) as conn:
        assert str(get_submission(conn, submission_id)["state"]) == "denied"
        assert get_balance(conn, GUILD_ID, REQUESTER) == 5000


@pytest.mark.asyncio
async def test_declining_from_the_board_refunds_exactly_once(ctx, db):
    """The money was taken at submit, so a denial is a refund path. Two mods
    landing on the same request must pay it back once."""
    _enable(db)
    submission_id = _theme(db)
    bot = _bot(ctx)

    for _ in range(2):
        interaction = _picker_interaction(
            bot, user=_member(admin=True, member_id=999),
            message=_ephemeral_message(),
        )
        await resolve_theme(
            interaction, submission_id, approve=False, deny_reason="too close"
        )

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, REQUESTER) == 5000


@pytest.mark.asyncio
async def test_a_legacy_bank_channel_card_still_resolves(ctx, db):
    """Nothing posts new ones, but the cards already sitting in bank channels
    stay clickable, so the resolve path still edits them in place."""
    _enable(db)
    submission_id = _theme(db)
    card = _legacy_card()
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999), message=card
    )

    await resolve_theme(
        interaction, submission_id, approve=False, deny_reason="too close"
    )

    card.edit.assert_awaited()
    interaction.edit_original_response.assert_not_awaited()
