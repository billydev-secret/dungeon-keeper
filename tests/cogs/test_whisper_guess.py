"""Cog-level: Guess button + select-dropdown flow."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import Whisper, WhisperConfig
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET = 1001, 2001
FEED = 8001


def _w(*, solved: bool = False, guesses_left: int = 3) -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time(), state="pending", solved=solved, exposed=False,
        guesses_left=guesses_left, channel_msg_id=88888, dm_msg_id=99999,
    )


def _cfg(role_id: int = 7001) -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=role_id, channel_id=FEED, log_channel_id=8002)


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
async def test_guess_button_role_not_configured_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
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
    interaction.guild = MagicMock()
    interaction.guild.get_role = MagicMock(return_value=None)
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
    interaction.guild = MagicMock()
    role = MagicMock()
    role.members = [FakeMember(id=TARGET)]  # only the target themselves
    interaction.guild.get_role = MagicMock(return_value=role)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "no other" in args[0].lower()


# ── Button-level: happy path ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_button_small_list_sends_select_no_pagination():
    from bot_modules.cogs.whisper_cog import WhisperGuessSelectView, WhisperGuessMemberSelect
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    role = MagicMock()
    role.members = _make_members(5)
    interaction.guild.get_role = MagicMock(return_value=role)
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
    interaction.guild = MagicMock()
    role = MagicMock()
    role.members = _make_members(30)
    interaction.guild.get_role = MagicMock(return_value=role)
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

    cfg_mock = MagicMock(channel_id=FEED)
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg_mock), \
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
         patch("bot_modules.cogs.whisper_cog._do_record_guess"):
        await sel.callback(interaction)

    edit_kwargs = interaction.response.edit_message.call_args.kwargs
    assert edit_kwargs["view"] is None
    assert "wrong" in edit_kwargs["content"].lower() or "left" in edit_kwargs["content"].lower()


@pytest.mark.asyncio
async def test_guess_select_exhausted_removes_guess_button_from_dm():
    from bot_modules.cogs.whisper_cog import WhisperShareButton, WhisperHideButton, WhisperGuessButton
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
         patch("bot_modules.cogs.whisper_cog._do_record_guess"):
        await sel.callback(interaction)

    dm_msg.edit.assert_awaited_once()
    edited_view = dm_msg.edit.call_args.kwargs["view"]
    button_types = [type(item) for item in edited_view.children]
    assert WhisperShareButton in button_types
    assert WhisperHideButton in button_types
    assert WhisperGuessButton not in button_types

    edit_kwargs = interaction.response.edit_message.call_args.kwargs
    assert edit_kwargs["view"] is None
    assert "no more" in edit_kwargs["content"].lower()
