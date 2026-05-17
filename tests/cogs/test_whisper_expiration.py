"""Cog-level: age-lock end-to-end + DM copy framing."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.services.whisper_models import Whisper, WhisperConfig
from bot_modules.services.whisper_service import LOCK_DURATION_SECONDS
from tests.fakes import FakeMember, fake_interaction

SENDER, TARGET = 1001, 2001
ROLE = 7001


def _w(*, created_at: float | None = None, **overrides) -> Whisper:
    defaults = dict(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time() if created_at is None else created_at,
        state="pending", solved=False, exposed=False,
        guesses_left=3, channel_msg_id=88888, dm_msg_id=99999,
    )
    defaults.update(overrides)
    return Whisper(**defaults)  # type: ignore[arg-type]


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=ROLE, channel_id=8001, log_channel_id=8002)


# ── Guess button rejects when whisper is age-locked ──────────────────────────


@pytest.mark.asyncio
async def test_guess_select_callback_rejects_locked_whisper():
    """Even if a stale ephemeral lingers, the guess select rejects on a locked whisper."""
    from bot_modules.cogs.whisper_cog import WhisperGuessMemberSelect
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    members = [FakeMember(id=4001, display_name="suspect")]
    sel = WhisperGuessMemberSelect(bot, 42, members, page=0)  # type: ignore[arg-type]
    sel._values = [str(SENDER)]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.edit_message = AsyncMock()

    locked = _w(created_at=time.time() - LOCK_DURATION_SECONDS - 100)
    with patch("bot_modules.cogs.whisper_cog._do_load_whisper", return_value=locked), \
         patch("bot_modules.cogs.whisper_cog._do_record_guess", return_value=True):
        await sel.callback(interaction)

    interaction.response.edit_message.assert_awaited_once()
    content = interaction.response.edit_message.call_args.kwargs["content"]
    assert "locked" in content.lower() or "too old" in content.lower()


# ── Inbox view surfaces "Locked" pill and omits Guess button ─────────────────


def test_inbox_locked_whisper_status_pill_says_locked():
    from bot_modules.cogs.whisper_cog import WhisperInboxSelectView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    locked = _w(created_at=time.time() - LOCK_DURATION_SECONDS - 100)
    view = WhisperInboxSelectView(bot, [locked], invoker_id=TARGET, mode="received")
    emb = view.embed()
    assert "Locked" in (emb.description or "")
    assert "too old" in (emb.footer.text or "").lower()


# ── DM copy reframe ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_dm_message_frames_game_with_guess_count():
    """The DM body now explicitly frames the whisper as a guessing game."""
    from bot_modules.cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)

    import discord as d
    role = MagicMock()
    role.id = ROLE
    sender = FakeMember(id=SENDER)
    sender.roles = [role]
    target = MagicMock()
    target.id = TARGET
    target.roles = [role]
    target.send = AsyncMock(return_value=MagicMock(id=12345))
    target.is_timed_out = MagicMock(return_value=False)

    feed_channel = MagicMock(spec=d.TextChannel)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=67890))

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test Server"
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_insert_whisper", return_value=42), \
         patch("bot_modules.cogs.whisper_cog._do_set_message_ids"):
        await cog._send_impl(interaction, target=target, message="my secret")

    target.send.assert_awaited_once()
    dm_body = target.send.call_args.args[0]
    assert "Whisper" in dm_body
    assert "guesses" in dm_body.lower()
    assert "Test Server" in dm_body
