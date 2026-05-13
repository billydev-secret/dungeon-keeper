"""Cog-level tests for the sticky channel prompt: /veil prompt, on_message
listener with debounce, and the VeilPromptView buttons."""
from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.veil_models import VeilConfig
from tests.fakes import FakeGuild, FakeMember, fake_interaction

VEIL_CHANNEL_ID = 8001
VEIL_ROLE_ID = 7001
GUILD_ID = 9001


def _make_cog(db_path: str = ":memory:"):
    from cogs.veil_cog import VeilCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    bot.add_view = MagicMock()
    return VeilCog(bot)


def _config(*, channel_id: int = VEIL_CHANNEL_ID, prompt_id: int = 0) -> VeilConfig:
    return VeilConfig(
        guild_id=GUILD_ID,
        veil_role_id=VEIL_ROLE_ID,
        veil_channel_id=channel_id,
        prompt_message_id=prompt_id,
    )


def _make_text_channel(channel_id: int = VEIL_CHANNEL_ID, *, send_returns_id: int = 99999):
    """A MagicMock that satisfies isinstance(..., discord.TextChannel)."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.mention = f"<#{channel_id}>"
    sent_msg = MagicMock()
    sent_msg.id = send_returns_id
    ch.send = AsyncMock(return_value=sent_msg)
    ch.fetch_message = AsyncMock()
    return ch


# ── VeilPromptView ───────────────────────────────────────────────────────────

def test_prompt_view_has_two_buttons_with_stable_custom_ids():
    from cogs.veil_cog import VeilPromptView

    view = VeilPromptView(MagicMock())
    children = cast(list[discord.ui.Button], view.children)
    custom_ids = {c.custom_id for c in children if c.custom_id}
    assert "veil_prompt_submit" in custom_ids
    assert "veil_prompt_help" in custom_ids
    assert len(children) == 2


def test_prompt_view_is_persistent():
    from cogs.veil_cog import VeilPromptView

    view = VeilPromptView(MagicMock())
    assert view.timeout is None


@pytest.mark.asyncio
async def test_prompt_submit_button_sends_ephemeral_instructions():
    from cogs.veil_cog import VeilPromptView

    view = VeilPromptView(MagicMock())
    children = cast(list[discord.ui.Button], view.children)
    submit_btn = next(c for c in children if c.custom_id == "veil_prompt_submit")
    interaction = fake_interaction()

    await submit_btn.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args.kwargs
    assert call_kwargs.get("ephemeral") is True
    msg = interaction.response.send_message.call_args.args[0]
    assert "/veil submit" in msg.lower()


@pytest.mark.asyncio
async def test_prompt_help_button_sends_ephemeral_rules():
    from cogs.veil_cog import VeilPromptView

    view = VeilPromptView(MagicMock())
    children = cast(list[discord.ui.Button], view.children)
    help_btn = next(c for c in children if c.custom_id == "veil_prompt_help")
    interaction = fake_interaction()

    await help_btn.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args.kwargs
    assert call_kwargs.get("ephemeral") is True
    msg = interaction.response.send_message.call_args.args[0]
    assert "veil" in msg.lower() and ("guess" in msg.lower() or "submit" in msg.lower())


# ── _repost_prompt helper ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repost_prompt_posts_when_no_prior():
    from cogs.veil_cog import _repost_prompt

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    channel = _make_text_channel(send_returns_id=12345)

    with patch("cogs.veil_cog._load_config", return_value=_config(prompt_id=0)), \
         patch("cogs.veil_cog._do_set_config") as set_cfg:
        await _repost_prompt(bot, channel, GUILD_ID)

    channel.fetch_message.assert_not_awaited()
    channel.send.assert_awaited_once()
    set_cfg.assert_called_once()
    saved_value = set_cfg.call_args.args[3]
    assert saved_value == "12345"


@pytest.mark.asyncio
async def test_repost_prompt_deletes_prior_then_posts():
    from cogs.veil_cog import _repost_prompt

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    channel = _make_text_channel(send_returns_id=20000)
    old_msg = MagicMock()
    old_msg.delete = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=old_msg)

    with patch("cogs.veil_cog._load_config", return_value=_config(prompt_id=10000)), \
         patch("cogs.veil_cog._do_set_config"):
        await _repost_prompt(bot, channel, GUILD_ID)

    channel.fetch_message.assert_awaited_once_with(10000)
    old_msg.delete.assert_awaited_once()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_repost_prompt_tolerates_missing_prior():
    from cogs.veil_cog import _repost_prompt

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    channel = _make_text_channel()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))

    with patch("cogs.veil_cog._load_config", return_value=_config(prompt_id=10000)), \
         patch("cogs.veil_cog._do_set_config"):
        await _repost_prompt(bot, channel, GUILD_ID)

    channel.send.assert_awaited_once()


# ── on_message listener ──────────────────────────────────────────────────────

def _make_message(*, channel_id: int, author_bot: bool = False, guild_id: int = GUILD_ID):
    msg = MagicMock()
    msg.author.bot = author_bot
    msg.author.id = 555
    guild = FakeGuild(id=guild_id)
    msg.guild = guild
    channel = _make_text_channel(channel_id=channel_id)
    msg.channel = channel
    return msg


@pytest.mark.asyncio
async def test_on_message_ignores_bot_authors():
    cog = _make_cog()
    msg = _make_message(channel_id=VEIL_CHANNEL_ID, author_bot=True)

    with patch("cogs.veil_cog._load_config") as load_cfg:
        await cog.on_message(msg)

    load_cfg.assert_not_called()
    assert not cog._pending_prompt_reposts


@pytest.mark.asyncio
async def test_on_message_ignores_dms():
    cog = _make_cog()
    msg = MagicMock()
    msg.author.bot = False
    msg.guild = None

    with patch("cogs.veil_cog._load_config") as load_cfg:
        await cog.on_message(msg)

    load_cfg.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_ignores_non_veil_channels():
    cog = _make_cog()
    msg = _make_message(channel_id=99999)  # not the veil channel

    with patch("cogs.veil_cog._load_config", return_value=_config()):
        await cog.on_message(msg)

    assert not cog._pending_prompt_reposts


@pytest.mark.asyncio
async def test_on_message_schedules_repost_for_veil_channel():
    cog = _make_cog()
    msg = _make_message(channel_id=VEIL_CHANNEL_ID)

    with patch("cogs.veil_cog._load_config", return_value=_config()):
        await cog.on_message(msg)

    task = cog._pending_prompt_reposts.get(GUILD_ID)
    assert task is not None
    assert not task.done()
    task.cancel()


@pytest.mark.asyncio
async def test_on_message_debounce_cancels_prior_pending_task():
    cog = _make_cog()
    msg = _make_message(channel_id=VEIL_CHANNEL_ID)

    with patch("cogs.veil_cog._load_config", return_value=_config()):
        await cog.on_message(msg)
        first_task = cog._pending_prompt_reposts[GUILD_ID]
        await cog.on_message(msg)
        second_task = cog._pending_prompt_reposts[GUILD_ID]

    # Yield once so the cancellation propagates from the inner sleep to
    # the task itself.
    await asyncio.sleep(0)

    assert first_task is not second_task
    assert first_task.cancelled() or first_task.done()
    second_task.cancel()


@pytest.mark.asyncio
async def test_cog_unload_cancels_pending_repost_tasks():
    cog = _make_cog()
    msg = _make_message(channel_id=VEIL_CHANNEL_ID)

    with patch("cogs.veil_cog._load_config", return_value=_config()):
        await cog.on_message(msg)

    task = cog._pending_prompt_reposts[GUILD_ID]
    await cog.cog_unload()  # type: ignore[attr-defined]

    # cog_unload calls cancel(); allow the event loop a tick for the
    # cancellation to register.
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert not cog._pending_prompt_reposts


# ── /veil prompt admin command ──────────────────────────────────────────────

async def _veil_prompt(cog, interaction):
    await cog.veil_prompt.callback(cog, interaction)


@pytest.mark.asyncio
async def test_veil_prompt_rejects_when_channel_unset():
    member = FakeMember(id=1001)
    guild = FakeGuild(id=GUILD_ID, members={member.id: member})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("cogs.veil_cog._load_config", return_value=_config(channel_id=0)):
        await _veil_prompt(cog, interaction)

    msg = interaction.followup.send.call_args.args[0]
    assert "not configured" in msg.lower() or "setup" in msg.lower()


@pytest.mark.asyncio
async def test_veil_prompt_posts_to_configured_channel():
    member = FakeMember(id=1001)
    channel = _make_text_channel()
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, channels={VEIL_CHANNEL_ID: channel})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("cogs.veil_cog._load_config", return_value=_config()), \
         patch("cogs.veil_cog._do_set_config"):
        await _veil_prompt(cog, interaction)

    channel.send.assert_awaited_once()
    msg = interaction.followup.send.call_args.args[0]
    assert "posted" in msg.lower() or "prompt" in msg.lower()
