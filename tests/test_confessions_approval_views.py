"""Tests for the confession approval queue's moderator surface.

``confessions/approval_views.py`` — the todo board's 🕵️ Confessions button, the
ephemeral picker behind it and the Approve / Reject card.

The property most of these exist to hold is the privacy seam. The board is
gated on the *moderator* tier, which is a wider circle than the admin-only
Confessions Audit Log — and that panel is admin-gated precisely because it puts
a real name to an anonymous post. So approving must never become a second,
wider way to de-anonymise: no author id may reach the picker, the option text,
the review card, or the board row. The rest covers the exactly-once claim (two
mods must not post the same confession twice) and what a member is told.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.confessions.approval_views import (
    ConfessionPickSelect,
    ConfessionReviewView,
    build_review_embed,
    notify_confession_expired,
    open_confessions_picker,
)
from bot_modules.core.db_utils import open_db
from bot_modules.services.confessions_service import (
    GuildConfig,
    enqueue_confession,
    pending_confession_count,
    pending_confessions,
    upsert_config,
)
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
AUTHOR = 424242
BODY = "I ate the last biscuit"


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    # A guild with approval on. Publishing reads this config back, so without
    # it every approve would take the "confessions aren't available" branch.
    upsert_config(
        db_path,
        GuildConfig(
            guild_id=GUILD_ID, dest_channel_id=777, log_channel_id=0,
            require_approval=True,
        ),
    )
    return db_path


@pytest.fixture(autouse=True)
def _patch_accent():
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        yield


def _queue(db, content=BODY, *, author_id=AUTHOR, notify=1, guild_id=GUILD_ID) -> int:
    return enqueue_confession(
        db, guild_id=guild_id, author_id=author_id, content=content,
        notify_original_author=notify,
    )


def _bot(db, *, is_mod=True, publish=None) -> MagicMock:
    bot = MagicMock()
    bot.ctx = SimpleNamespace(
        db_path=db,
        open_db=lambda: open_db(db),
        is_mod=MagicMock(return_value=is_mod),
    )
    bot.publish = publish or AsyncMock(return_value=(True, ""))
    bot.todo_refresh = AsyncMock(return_value=True)
    bot.fetch_user = AsyncMock(side_effect=discord.HTTPException(MagicMock(), ""))

    def _get_cog(name):
        if name == "ConfessionsCog":
            return SimpleNamespace(publish_confession=bot.publish)
        return SimpleNamespace(refresh_board=bot.todo_refresh)

    bot.get_cog = MagicMock(side_effect=_get_cog)
    return bot


def _member(member_id=999) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = False
    m.display_name = "Mod"
    return m


def _interaction(bot, *, members=()) -> MagicMock:
    guild = FakeGuild(id=GUILD_ID, members={m.id: m for m in members})
    i = fake_interaction(guild=guild)
    i.client = bot
    i.user = _member()
    return i


# ── the picker ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_picker_lists_the_queue_oldest_first(db):
    first = _queue(db, "first")
    second = _queue(db, "second")
    interaction = _interaction(_bot(db))

    await open_confessions_picker(interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    assert [o.value for o in view.children[0].options] == [str(first), str(second)]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_the_picker_refuses_a_non_mod(db):
    _queue(db)
    interaction = _interaction(_bot(db, is_mod=False))

    await open_confessions_picker(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "moderators" in msg.lower()
    assert "view" not in interaction.response.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_the_picker_says_so_when_nothing_is_waiting(db):
    interaction = _interaction(_bot(db))

    await open_confessions_picker(interaction)

    assert "waiting" in interaction.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_the_picker_is_scoped_to_its_guild(db):
    _queue(db, "elsewhere", guild_id=GUILD_ID + 1)
    interaction = _interaction(_bot(db))

    await open_confessions_picker(interaction)

    assert "view" not in interaction.response.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_no_picker_option_can_carry_an_author(db):
    _queue(db, author_id=AUTHOR)
    interaction = _interaction(_bot(db))

    await open_confessions_picker(interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    rendered = " ".join(
        f"{o.label} {o.description} {o.value}" for o in view.children[0].options
    )
    assert str(AUTHOR) not in rendered


# ── the review card ───────────────────────────────────────────────────


def test_the_review_card_shows_the_body_and_names_nobody():
    embed = build_review_embed(BODY, 1_700_000_000, discord.Color(0x123456))
    rendered = f"{embed.title} {embed.description} {embed.footer.text} " + " ".join(
        f"{f.name} {f.value}" for f in embed.fields
    )
    assert BODY in rendered
    assert str(AUTHOR) not in rendered
    # And it says why, rather than looking like a card that failed to load one.
    assert "anonymous" in embed.footer.text.lower()


@pytest.mark.asyncio
async def test_opening_a_confession_offers_approve_and_reject(db):
    pending_id = _queue(db)
    bot = _bot(db)
    interaction = _interaction(bot)
    select = ConfessionPickSelect([{"id": pending_id, "content": BODY, "created_at": 0}], now=0)
    select._values = [str(pending_id)]

    await select.callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert BODY in kwargs["embed"].description
    labels = {c.label for c in kwargs["view"].children}
    assert labels == {"Approve", "Reject"}


@pytest.mark.asyncio
async def test_a_confession_resolved_while_the_picker_was_open_offers_nothing(db):
    """Somebody else got there first — show that, rather than a decision that
    has already been made."""
    bot = _bot(db)
    interaction = _interaction(bot)
    select = ConfessionPickSelect([{"id": 404, "content": BODY, "created_at": 0}], now=0)
    select._values = ["404"]

    await select.callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["view"] is None
    assert "already been handled" in kwargs["embed"].description


# ── approving ─────────────────────────────────────────────────────────


async def _press(view, interaction, label):
    button = next(c for c in view.children if c.label == label)
    await button.callback(interaction)


@pytest.mark.asyncio
async def test_approving_publishes_the_confession_and_clears_the_queue(db):
    pending_id = _queue(db, author_id=AUTHOR, notify=1)
    bot = _bot(db)
    interaction = _interaction(bot)

    await _press(ConfessionReviewView(pending_id), interaction, "Approve")

    kwargs = bot.publish.await_args.kwargs
    assert kwargs["content"] == BODY
    assert kwargs["author_id"] == AUTHOR
    assert kwargs["notify"] is True
    with open_db(db) as conn:
        assert pending_confession_count(conn, GUILD_ID) == 0


@pytest.mark.asyncio
async def test_approving_repaints_the_board(db):
    pending_id = _queue(db)
    bot = _bot(db)

    await _press(ConfessionReviewView(pending_id), _interaction(bot), "Approve")

    bot.todo_refresh.assert_awaited_with(GUILD_ID)


@pytest.mark.asyncio
async def test_two_mods_approving_together_post_it_once(db):
    """The claim is the delete, in one immediate transaction, so the second
    press has nothing to act on."""
    pending_id = _queue(db)
    bot = _bot(db)
    view = ConfessionReviewView(pending_id)

    await _press(view, _interaction(bot), "Approve")
    second = _interaction(bot)
    await _press(view, second, "Approve")

    assert bot.publish.await_count == 1
    embed = second.edit_original_response.await_args.kwargs["embed"]
    assert "already been handled" in embed.description


@pytest.mark.asyncio
async def test_a_failed_post_puts_the_confession_back(db):
    """The row is claimed before the post, and cannot be un-deleted — so a
    permission that vanished since submission must not cost a member their
    text."""
    pending_id = _queue(db)
    bot = _bot(db, publish=AsyncMock(return_value=(False, "❌ Failed to post.")))
    interaction = _interaction(bot)

    await _press(ConfessionReviewView(pending_id), interaction, "Approve")

    with open_db(db) as conn:
        rows = pending_confessions(conn, GUILD_ID)
    assert [r["content"] for r in rows] == [BODY]
    assert bot.publish.await_count == 1
    assert "Put back in the queue" in (
        interaction.edit_original_response.await_args.kwargs["embed"].description
    )


@pytest.mark.asyncio
async def test_approving_refuses_a_non_mod(db):
    """Re-checked at click time: an ephemeral card outlives the roles of
    whoever opened it."""
    pending_id = _queue(db)
    bot = _bot(db, is_mod=False)
    interaction = _interaction(bot)

    await _press(ConfessionReviewView(pending_id), interaction, "Approve")

    assert bot.publish.await_count == 0
    with open_db(db) as conn:
        assert pending_confession_count(conn, GUILD_ID) == 1


# ── rejecting ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejecting_opens_the_reason_modal(db):
    pending_id = _queue(db)
    interaction = _interaction(_bot(db))

    await _press(ConfessionReviewView(pending_id), interaction, "Reject")

    modal = interaction.response.send_modal.await_args.args[0]
    assert modal.pending_id == pending_id
    # Optional: the common case is a confession that simply doesn't belong.
    assert modal.reason.required is False


@pytest.mark.asyncio
async def test_rejecting_refuses_a_non_mod(db):
    pending_id = _queue(db)
    interaction = _interaction(_bot(db, is_mod=False))

    await _press(ConfessionReviewView(pending_id), interaction, "Reject")

    assert interaction.response.send_modal.await_count == 0


@pytest.mark.asyncio
async def test_rejecting_dms_the_author_with_the_reason_and_clears_the_queue(db):
    from bot_modules.confessions.approval_views import RejectModal

    pending_id = _queue(db, author_id=AUTHOR)
    author = _member(AUTHOR)
    bot = _bot(db)
    interaction = _interaction(bot, members=[author])
    modal = RejectModal(pending_id)
    modal.reason._value = "Not here, sorry"

    with patch(
        "bot_modules.confessions.approval_views.send_branded_dm", new=AsyncMock()
    ) as dm:
        await modal.on_submit(interaction)

    assert dm.await_args.args[0] is author
    body = dm.await_args.kwargs["embed"].description
    assert "Not here, sorry" in body
    assert dm.await_args.kwargs["allowed_mentions"].everyone is False
    with open_db(db) as conn:
        assert pending_confession_count(conn, GUILD_ID) == 0


@pytest.mark.asyncio
async def test_a_rejection_does_not_publish(db):
    from bot_modules.confessions.approval_views import RejectModal

    pending_id = _queue(db)
    bot = _bot(db)
    with patch(
        "bot_modules.confessions.approval_views.send_branded_dm", new=AsyncMock()
    ):
        await RejectModal(pending_id).on_submit(_interaction(bot))

    assert bot.publish.await_count == 0


@pytest.mark.asyncio
async def test_a_closed_dm_does_not_undo_the_rejection(db):
    """The decision has landed; a member who blocked DMs cannot resurrect it."""
    from bot_modules.confessions.approval_views import RejectModal

    pending_id = _queue(db)
    bot = _bot(db)
    interaction = _interaction(bot, members=[_member(AUTHOR)])
    with patch(
        "bot_modules.confessions.approval_views.send_branded_dm",
        new=AsyncMock(side_effect=discord.HTTPException(MagicMock(), "")),
    ):
        await RejectModal(pending_id).on_submit(interaction)

    with open_db(db) as conn:
        assert pending_confession_count(conn, GUILD_ID) == 0


# ── expiry ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_expiry_dm_is_not_a_rejection(db):
    author = _member(AUTHOR)
    guild = FakeGuild(id=GUILD_ID, members={AUTHOR: author})
    with patch(
        "bot_modules.confessions.approval_views.send_branded_dm", new=AsyncMock()
    ) as dm:
        await notify_confession_expired(_bot(db), guild, AUTHOR)

    body = dm.await_args.kwargs["embed"].description
    assert "didn't approve" not in body
    assert "week" in body
