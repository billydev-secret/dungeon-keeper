"""Cog wiring for the music player.

Only the glue that has been wrong lives here: what a track change does to the
now-playing card. The card's own placement rules are covered by
``tests/test_music_now_playing.py`` and the pure helpers by
``tests/test_music_logic.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs import music_cog as music_cog_module
from bot_modules.cogs.music_cog import MusicCog
from bot_modules.services.music_now_playing import CardRefresher

GUILD = 7001
CHANNEL = 8001
CARD = 9001


@pytest.fixture
def cog(monkeypatch):
    monkeypatch.setattr(
        music_cog_module,
        "safe_resolve_accent",
        AsyncMock(return_value=discord.Color(0xC9A961)),
    )
    bot = SimpleNamespace(ctx=SimpleNamespace(db_path=":memory:"), user=None)
    cog = MusicCog(bot)  # type: ignore[arg-type]
    # No quiet window: this is about what a track change does, not when.
    cog._card = CardRefresher(interval=0.0)
    return cog


def _text_channel(channel_id: int = CHANNEL, *, message_id: int = CARD) -> MagicMock:
    """A TextChannel the cog's isinstance guard accepts, recording its calls."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    posted = MagicMock()
    posted.id = message_id
    posted.channel = channel
    channel.send = AsyncMock(return_value=posted)
    card = MagicMock()
    card.edit = AsyncMock()
    card.delete = AsyncMock()
    channel.get_partial_message = MagicMock(return_value=card)
    return channel


def _player(guild) -> MagicMock:
    player = MagicMock()
    player.guild = guild
    player.paused = False
    return player


def _track(title: str) -> SimpleNamespace:
    return SimpleNamespace(
        title=title, author="somebody", uri=f"https://x/{title}", length=1000
    )


@pytest.mark.asyncio
async def test_a_track_change_edits_the_card_instead_of_posting_a_second_one(cog):
    """The reported bug, at the seam where it lived.

    ``on_wavelink_track_start`` used to ``channel.send`` unconditionally, so
    every track left another card in the channel and the one being watched
    kept naming a track that had finished.
    """
    channel = _text_channel()
    guild = MagicMock()
    guild.id = GUILD
    guild.get_channel.return_value = channel
    guild.get_member.return_value = None
    player = _player(guild)
    queue = cog._queue(GUILD)
    queue.text_channel_id = CHANNEL

    await cog._refresh_now_playing(player, _track("first"))
    await cog._refresh_now_playing(player, _track("second"))

    channel.send.assert_awaited_once()
    card = channel.get_partial_message(CARD)
    card.edit.assert_awaited_once()
    assert "second" in card.edit.await_args.kwargs["embed"].title


@pytest.mark.asyncio
async def test_no_text_channel_on_record_means_no_card(cog):
    guild = MagicMock()
    guild.id = GUILD
    player = _player(guild)
    cog._queue(GUILD)  # text_channel_id stays None until someone runs /play

    await cog._refresh_now_playing(player, _track("first"))
    guild.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_the_card_stays_in_its_own_channel_when_play_moves_elsewhere(cog):
    """Moving it would strand the old card, buttons and all."""
    card_channel = _text_channel(CHANNEL)
    guild = MagicMock()
    guild.id = GUILD
    guild.get_channel.side_effect = lambda cid: (
        card_channel if cid == CHANNEL else _text_channel(cid, message_id=cid)
    )
    guild.get_member.return_value = None
    player = _player(guild)
    queue = cog._queue(GUILD)
    queue.text_channel_id = CHANNEL

    await cog._refresh_now_playing(player, _track("first"))
    queue.text_channel_id = 8123  # someone ran /play from another channel
    await cog._refresh_now_playing(player, _track("second"))

    card_channel.send.assert_awaited_once()
    card_channel.get_partial_message(CARD).edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ending_the_session_takes_the_card_with_it(cog):
    """Left behind, the card names a stopped track and its buttons still work
    — and the next /play posts a second one beside it, which is how the
    channel filled up with cards in the first place."""
    channel = _text_channel()
    guild = MagicMock()
    guild.id = GUILD
    guild.get_channel.return_value = channel
    queue = cog._queue(GUILD)
    queue.now_playing_message_id = CARD
    queue.now_playing_channel_id = CHANNEL

    await cog._end_session(guild)

    channel.get_partial_message(CARD).delete.assert_awaited_once()
    assert cog._queue(GUILD).now_playing_message_id is None


@pytest.mark.asyncio
async def test_ending_a_session_that_never_had_a_card_is_a_no_op(cog):
    guild = MagicMock()
    guild.id = GUILD
    await cog._end_session(guild)  # no queue at all — must not raise
    guild.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_nowplaying_posts_the_new_card_before_deleting_the_old(cog):
    """Retiring first left a window across two awaits where the queue held no
    card — a track change landing in it would post a third one."""
    old_channel = _text_channel(CHANNEL)
    guild = MagicMock()
    guild.id = GUILD
    guild.get_channel.return_value = old_channel
    guild.get_member.return_value = None
    player = _player(guild)
    cog._player = MagicMock(return_value=player)
    queue = cog._queue(GUILD)
    queue.current = _track("first")
    queue.now_playing_message_id = CARD
    queue.now_playing_channel_id = CHANNEL

    order: list[str] = []
    new_message = MagicMock(id=CARD + 5)
    new_message.channel = MagicMock(id=8123)

    async def _send(**_kw):
        order.append("send")
        # Mid-flight the old card is still the one on record, so a refresh
        # arriving here edits it rather than posting a rival.
        assert queue.now_playing_message_id == CARD

    interaction = MagicMock()
    interaction.guild = guild
    interaction.response.send_message = _send
    interaction.original_response = AsyncMock(return_value=new_message)
    old_channel.get_partial_message(CARD).delete = AsyncMock(
        side_effect=lambda: order.append("delete")
    )

    await cog.now_playing_cmd.callback(cog, interaction)

    assert order == ["send", "delete"]
    assert queue.now_playing_message_id == CARD + 5
    assert queue.now_playing_channel_id == 8123


@pytest.mark.asyncio
async def test_nowplaying_cancels_a_queued_refresh_for_the_old_channel(cog):
    """That refresh closed over the old channel. Once the ids move it stops
    matching, so it would decide there is no card there and post one."""
    cog._card = CardRefresher(interval=60.0)
    channel = _text_channel(CHANNEL)
    guild = MagicMock()
    guild.id = GUILD
    guild.get_channel.return_value = channel
    guild.get_member.return_value = None
    player = _player(guild)
    cog._player = MagicMock(return_value=player)
    queue = cog._queue(GUILD)
    queue.current = _track("first")
    queue.text_channel_id = CHANNEL

    # A track change, then a second one that has to queue behind it.
    await cog._refresh_now_playing(player, _track("first"))
    await cog._refresh_now_playing(player, _track("second"))
    assert cog._card._pending.get(GUILD) is not None

    new_message = MagicMock(id=CARD + 5)
    new_message.channel = MagicMock(id=CHANNEL)
    interaction = MagicMock()
    interaction.guild = guild
    interaction.response.send_message = AsyncMock()
    interaction.original_response = AsyncMock(return_value=new_message)

    await cog.now_playing_cmd.callback(cog, interaction)
    assert cog._card._pending.get(GUILD) is None


@pytest.mark.asyncio
async def test_the_bot_being_pulled_out_of_voice_ends_the_session(cog):
    """Nothing used to notice: the listener ignored every bot, so the queue
    survived with the card's channel still on it — and the next /play from a
    different channel kept editing the card in the old one while the people
    who started the new session saw nothing."""
    channel = _text_channel()
    guild = MagicMock()
    guild.id = GUILD
    guild.get_channel.return_value = channel
    queue = cog._queue(GUILD)
    queue.now_playing_message_id = CARD
    queue.now_playing_channel_id = CHANNEL

    me = MagicMock()
    me.id = 55
    cog.bot.user = me
    member = MagicMock()
    member.id = 55
    member.guild = guild

    await cog.on_voice_state_update(
        member, MagicMock(channel=MagicMock()), MagicMock(channel=None)
    )

    assert GUILD not in cog._queues
    channel.get_partial_message(CARD).delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_bot_moving_between_voice_channels_is_not_a_session_end(cog):
    """A drag from one voice channel to another keeps playing."""
    guild = MagicMock()
    guild.id = GUILD
    cog._queue(GUILD)

    me = MagicMock()
    me.id = 55
    cog.bot.user = me
    member = MagicMock()
    member.id = 55
    member.guild = guild

    await cog.on_voice_state_update(
        member, MagicMock(channel=MagicMock()), MagicMock(channel=MagicMock())
    )
    assert GUILD in cog._queues
