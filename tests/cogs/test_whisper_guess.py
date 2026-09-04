"""Cog-level: Guess button + select-dropdown flow."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import Whisper, WhisperConfig
from bot_modules.services.whisper_service import GuessOutcome
from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction

SENDER, TARGET = 1001, 2001
FEED = 8001


def _w(*, solved: bool = False, guesses_left: int = 3) -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time(), state="pending", solved=solved, exposed=False,
        guesses_left=guesses_left, channel_msg_id=88888, dm_msg_id=99999,
    )


ROLE = 7001


def _cfg(role_id: int = ROLE, *, sender_feedback: bool = False) -> WhisperConfig:
    return WhisperConfig(
        guild_id=9001, role_id=role_id, channel_id=FEED, log_channel_id=8002,
        sender_feedback=sender_feedback,
    )


def _pool_member(uid: int, **kw) -> FakeMember:
    """A member holding the Whisper role — what the native picker must accept."""
    return FakeMember(id=uid, roles=[FakeRole(id=ROLE)], **kw)


def _make_guess_button(whisper_id: int = 42):
    from bot_modules.cogs.whisper_cog import WhisperGuessButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperGuessButton(bot, whisper_id)


def _make_members(n: int, exclude_id: int = TARGET) -> list[FakeMember]:
    return [
        FakeMember(id=5000 + i, display_name=f"Member{i:03d}")
        for i in range(n)
        if (5000 + i) != exclude_id
    ]


# ── Button-level: pre-checks ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_button_non_target_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "recipient" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_already_solved_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(solved=True)):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_guess_button_no_guesses_left_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(guesses_left=0)):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_guess_button_no_guild_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    interaction.response.send_message = AsyncMock()
    button.bot.get_guild = MagicMock(return_value=None)

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_guild_context_uses_user_select():
    """In a guild interaction the native avatar picker (UserSelect) is shown
    instead of the string-based picker."""
    from bot_modules.cogs.whisper_cog import (
        WhisperGuessUserSelect,
        WhisperGuessUserSelectView,
    )
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    view = kwargs["view"]
    assert isinstance(view, WhisperGuessUserSelectView)
    assert any(isinstance(c, WhisperGuessUserSelect) for c in view.children)


# The string-based picker (with role/member lookup + pagination) is the DM
# fallback now — auto-populated user selects can't resolve guild members from a
# DM. These tests run in DM context (interaction.guild is None; the guild is
# resolved server-side via bot.get_guild).


@pytest.mark.asyncio
async def test_guess_button_role_not_configured_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    button.bot.get_guild = MagicMock(return_value=MagicMock())
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(role_id=0)):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "role" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_role_missing_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    guild = MagicMock()
    guild.get_role = MagicMock(return_value=None)
    button.bot.get_guild = MagicMock(return_value=guild)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "role" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_empty_member_list_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    guild = MagicMock()
    role = MagicMock()
    role.members = [FakeMember(id=TARGET)]  # only the target themselves
    guild.get_role = MagicMock(return_value=role)
    button.bot.get_guild = MagicMock(return_value=guild)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "no other" in args[0].lower()


# ── Button-level: happy path (DM string picker) ───────────────────────────────

@pytest.mark.asyncio
async def test_guess_button_small_list_sends_select_no_pagination():
    from bot_modules.cogs.whisper_cog import WhisperGuessSelectView, WhisperGuessMemberSelect
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    guild = MagicMock()
    role = MagicMock()
    role.members = _make_members(5)
    guild.get_role = MagicMock(return_value=role)
    button.bot.get_guild = MagicMock(return_value=guild)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await button.callback(interaction)

    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    view = kwargs["view"]
    assert isinstance(view, WhisperGuessSelectView)
    item_types = [type(c) for c in view.children]
    assert WhisperGuessMemberSelect in item_types
    # No pagination buttons for ≤25 members (filter button is always present)
    pagination_labels = {"◀", "▶"}
    btn_labels = {c.label for c in view.children if isinstance(c, discord.ui.Button)}
    assert not (btn_labels & pagination_labels)


@pytest.mark.asyncio
async def test_guess_button_large_list_sends_select_with_pagination():
    from bot_modules.cogs.whisper_cog import WhisperGuessSelectView
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    guild = MagicMock()
    role = MagicMock()
    role.members = _make_members(30)
    guild.get_role = MagicMock(return_value=role)
    button.bot.get_guild = MagicMock(return_value=guild)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await button.callback(interaction)

    _, kwargs = interaction.response.send_message.call_args
    view = kwargs["view"]
    assert isinstance(view, WhisperGuessSelectView)
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    # prev + next pagination buttons + filter button
    assert len(buttons) == 3


# ── Native avatar picker (UserSelect) callback ───────────────────────────────

def _make_user_select(whisper_id: int = 42):
    from bot_modules.cogs.whisper_cog import WhisperGuessUserSelect
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperGuessUserSelect(bot, whisper_id)


@pytest.mark.asyncio
async def test_user_select_rejects_bot_without_consuming_guess():
    sel = _make_user_select()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    picked_bot = FakeMember(id=4242, bot=True)

    with patch.object(type(sel), "values", new_callable=PropertyMock,
                      return_value=[picked_bot]), \
         patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_record_guess") as rec:
        await sel.callback(interaction)

    rec.assert_not_called()  # a stray bot pick must not burn an attempt
    edit_kwargs = interaction.response.edit_message.call_args.kwargs
    assert edit_kwargs["view"] is None
    assert "bot" in edit_kwargs["content"].lower()


@pytest.mark.asyncio
async def test_user_select_rejects_member_outside_pool_without_consuming_guess():
    """rotation-rooms-167: the native picker offers every member of the
    server, so a pick with no Whisper role cannot be the sender. It is
    refused as a free pass, not a burned third of the target's guesses."""
    sel = _make_user_select()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    outsider = FakeMember(id=4343, roles=[FakeRole(id=1)])

    with patch.object(type(sel), "values", new_callable=PropertyMock,
                      return_value=[outsider]), \
         patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_record_guess") as rec:
        await sel.callback(interaction)

    rec.assert_not_called()
    edit_kwargs = interaction.response.edit_message.call_args.kwargs
    assert edit_kwargs["view"] is None
    assert edit_kwargs["content"] == "❌ They aren't in the Whisper pool — that one's free."


@pytest.mark.asyncio
async def test_user_select_correct_records_and_posts_to_feed():
    sel = _make_user_select()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.response.edit_message = AsyncMock()

    with patch.object(type(sel), "values", new_callable=PropertyMock,
                      return_value=[_pool_member(SENDER)]), \
         patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_record_guess") as rec:
        await sel.callback(interaction)

    rec.assert_called_once_with(":memory:", whisper_id=42, guessed_id=SENDER, correct=True)
    feed_channel.send.assert_awaited_once()
    edit_kwargs = interaction.response.edit_message.call_args.kwargs
    assert edit_kwargs["view"] is None


# ── Navigation ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_select_next_advances_page():
    from bot_modules.cogs.whisper_cog import WhisperGuessSelectView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    members = _make_members(30)
    view = WhisperGuessSelectView(bot, 42, members)  # type: ignore[arg-type]

    next_btn = next(c for c in view.children if isinstance(c, discord.ui.Button) and c.label == "▶")
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    await next_btn.callback(interaction)

    _, kwargs = interaction.response.edit_message.call_args
    new_view = kwargs["view"]
    assert isinstance(new_view, WhisperGuessSelectView)
    assert new_view._page == 1


@pytest.mark.asyncio
async def test_guess_select_prev_retreats_page():
    from bot_modules.cogs.whisper_cog import WhisperGuessSelectView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    members = _make_members(30)
    view = WhisperGuessSelectView(bot, 42, members, page=1)  # type: ignore[arg-type]

    prev_btn = next(c for c in view.children if isinstance(c, discord.ui.Button) and c.label == "◀")
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    await prev_btn.callback(interaction)

    _, kwargs = interaction.response.edit_message.call_args
    new_view = kwargs["view"]
    assert new_view._page == 0


# ── Select callback: outcomes ─────────────────────────────────────────────────

def _make_select(whisper_id: int = 42, members=None):
    from bot_modules.cogs.whisper_cog import WhisperGuessMemberSelect
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    if members is None:
        members = _make_members(3)
    sel = WhisperGuessMemberSelect(bot, whisper_id, members, page=0)  # type: ignore[arg-type]
    sel._values = [str(SENDER)]
    return sel


@pytest.mark.asyncio
async def test_guess_select_correct_posts_to_feed_and_edits_message():
    sel = _make_select()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.guild.get_member = MagicMock(
        return_value=FakeMember(id=SENDER, display_name="Sender")
    )
    interaction.response.edit_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_record_guess") as rec:
        await sel.callback(interaction)

    rec.assert_called_once_with(":memory:", whisper_id=42, guessed_id=SENDER, correct=True)
    feed_channel.send.assert_awaited_once()
    edit_kwargs = interaction.response.edit_message.call_args.kwargs
    assert edit_kwargs["view"] is None
    assert "solved" in edit_kwargs["content"].lower() or "right" in edit_kwargs["content"].lower()


@pytest.mark.asyncio
async def test_guess_select_wrong_shows_remaining_count():
    sel = _make_select()
    sel._values = ["9999"]  # wrong guess
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.edit_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(guesses_left=3)), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_record_guess"):
        await sel.callback(interaction)

    edit_kwargs = interaction.response.edit_message.call_args.kwargs
    assert edit_kwargs["view"] is None
    assert "wrong" in edit_kwargs["content"].lower() or "left" in edit_kwargs["content"].lower()


@pytest.mark.asyncio
async def test_guess_select_exhausted_removes_guess_button_from_dm():
    from bot_modules.cogs.whisper_cog import WhisperShareButton, WhisperDeleteButton, WhisperGuessButton
    sel = _make_select()
    sel._values = ["9999"]  # wrong, final guess
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.edit_message = AsyncMock()

    dm_msg = MagicMock()
    dm_msg.edit = AsyncMock()
    dm_channel = MagicMock()
    dm_channel.fetch_message = AsyncMock(return_value=dm_msg)
    interaction.user.create_dm = AsyncMock(return_value=dm_channel)

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w(guesses_left=1)), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_record_guess"):
        await sel.callback(interaction)

    dm_msg.edit.assert_awaited_once()
    edited_view = dm_msg.edit.call_args.kwargs["view"]
    button_types = [type(item) for item in edited_view.children]
    assert WhisperShareButton in button_types
    assert WhisperDeleteButton in button_types
    assert WhisperGuessButton not in button_types

    edit_kwargs = interaction.response.edit_message.call_args.kwargs
    assert edit_kwargs["view"] is None
    assert "no more" in edit_kwargs["content"].lower()


# ── Sender feedback DM (2026-09 review, rotation-rooms-159) ──────────────────
#
# The copy is pinned in tests/test_whisper_logic.py; these cover the cog's
# gates around it: the dial, the sender's pool membership, the no-contact
# degrade, and that the outcome path actually calls it.

WRONG = GuessOutcome(correct=False, attempts_remaining=2, exhausted=False)


def _guild_with(*members: FakeMember) -> FakeGuild:
    return FakeGuild(id=9001, members={m.id: m for m in members})


async def _notify(guild, cfg, *, guessed_id=4444, blocked=False):
    from bot_modules.cogs.whisper_cog import _notify_sender_of_guess

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    with patch("bot_modules.cogs.whisper_cog.send_branded_dm", AsyncMock()) as dm, \
         patch("bot_modules.cogs.whisper_cog.no_contact_service.is_no_contact",
               return_value=blocked), \
         patch("bot_modules.cogs.whisper_cog.build_name_fn",
               AsyncMock(return_value=lambda uid: f"User{uid}")):
        await _notify_sender_of_guess(
            bot, guild, cfg, _w(), guessed_id=guessed_id, outcome=WRONG,
        )
    return dm


@pytest.mark.asyncio
async def test_sender_is_dmed_with_the_guessed_name_when_the_dial_is_on():
    sender = _pool_member(SENDER)
    dm = await _notify(_guild_with(sender), _cfg(sender_feedback=True))

    dm.assert_awaited_once()
    assert dm.call_args.args[0] is sender
    embed = dm.call_args.kwargs["embed"]
    assert embed.description == "Whisper #42 — they guessed User4444. Wrong, 2 left."


@pytest.mark.parametrize(
    ("cfg", "sender"),
    [
        pytest.param(_cfg(sender_feedback=False), _pool_member(SENDER), id="dial-off"),
        pytest.param(_cfg(sender_feedback=True), FakeMember(id=SENDER), id="sender-opted-out"),
        pytest.param(_cfg(sender_feedback=True), None, id="sender-left-the-server"),
    ],
)
@pytest.mark.asyncio
async def test_sender_is_not_dmed(cfg, sender):
    """Ships dark, and honours ``/whisper optout`` — a sender who dropped the
    role has left the game and its DMs with it."""
    guild = _guild_with(sender) if sender is not None else _guild_with()
    dm = await _notify(guild, cfg)
    dm.assert_not_called()


@pytest.mark.asyncio
async def test_no_contact_pair_degrades_the_guessed_name_to_someone():
    """Naming a blocked party to the other side is contact. The degraded
    line reads like any other wrong guess, so the sender can't tell."""
    dm = await _notify(
        _guild_with(_pool_member(SENDER)), _cfg(sender_feedback=True), blocked=True,
    )
    embed = dm.call_args.kwargs["embed"]
    assert embed.description == "Whisper #42 — they guessed someone. Wrong, 2 left."
    assert "4444" not in embed.description


@pytest.mark.asyncio
async def test_guess_outcome_notifies_the_sender_after_answering_the_target():
    """One wiring assertion: the outcome helper reaches the notifier with the
    recorded guess, and only after the target's own reply has gone out."""
    from bot_modules.cogs.whisper_cog import _handle_guess_outcome

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    guild = _guild_with(_pool_member(SENDER))
    interaction = fake_interaction(user=FakeMember(id=TARGET), guild=guild)
    order: list[str] = []
    interaction.response.edit_message = AsyncMock(side_effect=lambda **_: order.append("target"))
    notify = AsyncMock(side_effect=lambda *a, **k: order.append("sender"))
    cfg = _cfg(sender_feedback=True)
    whisper = _w()

    with patch("bot_modules.cogs.whisper_cog._do_record_guess", return_value=True), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg), \
         patch("bot_modules.cogs.whisper_cog._notify_sender_of_guess", notify):
        await _handle_guess_outcome(interaction, bot, whisper, 4444)

    notify.assert_awaited_once_with(bot, guild, cfg, whisper, guessed_id=4444, outcome=WRONG)
    assert order == ["target", "sender"]
