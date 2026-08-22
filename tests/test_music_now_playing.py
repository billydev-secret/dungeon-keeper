"""Tests for the now-playing card's placement and refresh coalescing.

Covers ``bot_modules/services/music_now_playing.py`` — one card per guild,
edited in place, reposted only when it is gone. The embed's contents and the
button state are the ``build_embed`` / ``refresh_for`` half of that module and
are exercised here only as far as the card plumbing carries them.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.services.music_now_playing import (
    CardRefresher,
    render_card,
    retire_card,
)
from bot_modules.services.music_queue import GuildQueue

GUILD = 7001
CHANNEL = 8001
OTHER_CHANNEL = 8002
CARD = 9001

_EMBED = discord.Embed(title="a track")


def _http_error(status: int, cls=discord.HTTPException) -> Exception:
    response = MagicMock()
    response.status = status
    return cls(response, "boom")


class FakeChannel:
    """A text channel that records sends and edits, nothing more."""

    def __init__(self, channel_id: int = CHANNEL, *, next_message_id: int = CARD):
        self.id = channel_id
        self._next_message_id = next_message_id
        self.sent: list[discord.Embed] = []
        self.partials: dict[int, MagicMock] = {}

    def get_partial_message(self, message_id: int) -> MagicMock:
        partial = self.partials.get(message_id)
        if partial is None:
            partial = MagicMock()
            partial.id = message_id
            partial.edit = AsyncMock()
            partial.delete = AsyncMock()
            self.partials[message_id] = partial
        return partial

    async def send(self, *, embed, view):
        self.sent.append(embed)
        message = MagicMock()
        message.id = self._next_message_id
        self._next_message_id += 1
        return message


def _queue() -> GuildQueue:
    return GuildQueue(guild_id=GUILD, text_channel_id=CHANNEL)


# ── render_card ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_render_posts_the_card_and_records_where_it_is():
    channel, queue = FakeChannel(), _queue()
    await render_card(channel, queue, embed=_EMBED, view=MagicMock())
    assert len(channel.sent) == 1
    assert queue.now_playing_message_id == CARD
    assert queue.now_playing_channel_id == CHANNEL


@pytest.mark.asyncio
async def test_the_next_track_edits_the_same_card_instead_of_posting_another():
    """The reported bug: a track change left the card you were watching stale.

    Every track start used to ``send``, so a queue's worth of tracks left a
    queue's worth of cards — each with live buttons, all but the last naming a
    track that had finished.
    """
    channel, queue = FakeChannel(), _queue()
    await render_card(channel, queue, embed=_EMBED, view=MagicMock())
    second = discord.Embed(title="the next track")
    await render_card(channel, queue, embed=second, view=MagicMock())

    assert len(channel.sent) == 1, "a second card was posted"
    channel.get_partial_message(CARD).edit.assert_awaited_once()
    assert channel.get_partial_message(CARD).edit.await_args.kwargs["embed"] is second
    assert queue.now_playing_message_id == CARD


@pytest.mark.asyncio
async def test_a_deleted_card_is_reposted():
    channel, queue = FakeChannel(), _queue()
    await render_card(channel, queue, embed=_EMBED, view=MagicMock())
    channel.get_partial_message(CARD).edit.side_effect = _http_error(
        404, discord.NotFound
    )

    await render_card(channel, queue, embed=_EMBED, view=MagicMock())
    assert len(channel.sent) == 2
    assert queue.now_playing_message_id == CARD + 1


@pytest.mark.asyncio
async def test_a_transient_edit_failure_keeps_the_card_rather_than_piling_on():
    """A rate limit must not turn one card into two."""
    channel, queue = FakeChannel(), _queue()
    await render_card(channel, queue, embed=_EMBED, view=MagicMock())
    channel.get_partial_message(CARD).edit.side_effect = _http_error(429)

    await render_card(channel, queue, embed=_EMBED, view=MagicMock())
    assert len(channel.sent) == 1
    assert queue.now_playing_message_id == CARD


@pytest.mark.asyncio
async def test_a_card_in_another_channel_is_left_alone_and_a_new_one_posted():
    channel, queue = FakeChannel(), _queue()
    queue.now_playing_message_id = 4242
    queue.now_playing_channel_id = OTHER_CHANNEL

    await render_card(channel, queue, embed=_EMBED, view=MagicMock())
    assert len(channel.sent) == 1
    assert queue.now_playing_channel_id == CHANNEL


@pytest.mark.asyncio
async def test_a_failed_first_post_records_no_card():
    channel, queue = FakeChannel(), _queue()
    channel.send = AsyncMock(side_effect=_http_error(403, discord.Forbidden))
    await render_card(channel, queue, embed=_EMBED, view=MagicMock())
    assert queue.now_playing_message_id is None
    assert queue.now_playing_channel_id is None


# ── retire_card ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retire_deletes_the_card_it_is_given():
    channel = FakeChannel()
    guild = MagicMock()
    guild.get_channel.return_value = channel

    await retire_card(guild, CHANNEL, CARD)
    channel.get_partial_message(CARD).delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_retire_swallows_an_already_deleted_card():
    """The common case — somebody cleared it by hand — is not an error."""
    channel = FakeChannel()
    channel.get_partial_message(CARD).delete.side_effect = _http_error(
        404, discord.NotFound
    )
    guild = MagicMock()
    guild.get_channel.return_value = channel

    await retire_card(guild, CHANNEL, CARD)  # must not raise


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "message_id"),
    [
        pytest.param(None, None, id="never-posted"),
        pytest.param(CHANNEL, None, id="channel-only"),
        pytest.param(None, CARD, id="message-only"),
    ],
)
async def test_retire_with_no_card_on_record_does_nothing(channel_id, message_id):
    guild = MagicMock()
    await retire_card(guild, channel_id, message_id)
    guild.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_retire_survives_a_channel_that_no_longer_exists():
    guild = MagicMock()
    guild.get_channel.return_value = None
    guild.get_thread.return_value = None

    await retire_card(guild, CHANNEL, CARD)  # must not raise


# ── CardRefresher ──────────────────────────────────────────────────────

INTERVAL = 0.05


def _counter():
    calls: list[str] = []

    def render(label: str):
        async def _run() -> None:
            calls.append(label)

        return _run

    return calls, render


@pytest.mark.asyncio
async def test_the_first_refresh_renders_straight_away():
    calls, render = _counter()
    refresher = CardRefresher(interval=INTERVAL)
    await refresher.submit(GUILD, render("a"))
    assert calls == ["a"]
    refresher.cancel_all()


@pytest.mark.asyncio
async def test_a_burst_costs_one_extra_render_showing_the_newest_state():
    """A member leaning on Skip must not cost one edit per press."""
    calls, render = _counter()
    refresher = CardRefresher(interval=INTERVAL)
    for label in ("first", "second", "third", "fourth"):
        await refresher.submit(GUILD, render(label))
    assert calls == ["first"], "the burst was not coalesced"

    await asyncio.sleep(INTERVAL * 3)
    assert calls == ["first", "fourth"]
    refresher.cancel_all()


@pytest.mark.asyncio
async def test_two_guilds_do_not_share_a_quiet_window():
    calls, render = _counter()
    refresher = CardRefresher(interval=INTERVAL)
    await refresher.submit(GUILD, render("a"))
    await refresher.submit(GUILD + 1, render("b"))
    assert calls == ["a", "b"]
    refresher.cancel_all()


@pytest.mark.asyncio
async def test_a_refresh_that_raises_does_not_strand_the_next_one():
    calls, render = _counter()
    refresher = CardRefresher(interval=INTERVAL)

    async def _boom() -> None:
        raise RuntimeError("discord fell over")

    await refresher.submit(GUILD, render("a"))
    await refresher.submit(GUILD, _boom)
    await asyncio.sleep(INTERVAL * 3)

    await refresher.submit(GUILD, render("c"))
    await asyncio.sleep(INTERVAL * 3)
    assert calls == ["a", "c"]
    refresher.cancel_all()


@pytest.mark.asyncio
async def test_forgetting_a_guild_drops_its_queued_refresh():
    """The session ended and took the queue with it; the pending edit is moot."""
    calls, render = _counter()
    refresher = CardRefresher(interval=INTERVAL)
    await refresher.submit(GUILD, render("a"))
    await refresher.submit(GUILD, render("b"))
    refresher.forget(GUILD)

    await asyncio.sleep(INTERVAL * 3)
    assert calls == ["a"]
    # And the next session starts on a clean window, not the old one's.
    await refresher.submit(GUILD, render("c"))
    assert calls == ["a", "c"]
    refresher.cancel_all()


@pytest.mark.asyncio
async def test_cancel_all_drops_every_guild_s_queued_refresh():
    calls, render = _counter()
    refresher = CardRefresher(interval=INTERVAL)
    for guild_id in (GUILD, GUILD + 1):
        await refresher.submit(guild_id, render(f"{guild_id}-first"))
        await refresher.submit(guild_id, render(f"{guild_id}-queued"))
    refresher.cancel_all()

    await asyncio.sleep(INTERVAL * 3)
    assert calls == [f"{GUILD}-first", f"{GUILD + 1}-first"]


@pytest.mark.asyncio
async def test_a_cancelled_flush_does_not_evict_its_replacement():
    """A cancel that lands *mid-render* unwinds after the next task is armed.

    The doomed task's cleanup then popped a slot that no longer belonged to
    it, evicting the live replacement: that task kept running, the slot read
    empty, and the next ``_arm`` started a second coalescer editing one card.
    """
    calls, render = _counter()
    refresher = CardRefresher(interval=INTERVAL)
    gate = asyncio.Event()

    async def _blocked() -> None:
        await gate.wait()
        calls.append("blocked")

    await refresher.submit(GUILD, render("first"))
    await refresher.submit(GUILD, _blocked)
    doomed = refresher._tasks[GUILD]
    await asyncio.sleep(INTERVAL * 2)  # doomed is now inside _blocked

    refresher.forget(GUILD)
    await refresher.submit(GUILD, render("second"))
    await refresher.submit(GUILD, render("queued-again"))
    replacement = refresher._tasks[GUILD]
    assert replacement is not doomed

    gate.set()  # let the cancellation finish unwinding
    await asyncio.sleep(0)
    assert doomed.done()
    assert not replacement.done()
    assert refresher._tasks.get(GUILD) is replacement, "the live task was evicted"

    await asyncio.sleep(INTERVAL * 3)
    assert calls[-1] == "queued-again", "the replacement never flushed"
    refresher.cancel_all()


@pytest.mark.asyncio
async def test_a_render_in_flight_when_drain_lands_still_finishes():
    """``render_card`` posts before it records the id it posted.

    A cancel landing between those two would leave the message in the channel
    with nothing pointing at it — a card nobody can edit and nobody deletes.
    So the render is shielded, and ``drain`` waits for it: the caller that goes
    on to read those ids has to see the ones the render just wrote.
    """
    refresher = CardRefresher(interval=INTERVAL)
    gate = asyncio.Event()
    posted: list[str] = []

    async def _slow_post() -> None:
        await gate.wait()
        posted.append("recorded the id")

    await refresher.submit(GUILD, lambda: asyncio.sleep(0))
    await refresher.submit(GUILD, _slow_post)
    await asyncio.sleep(INTERVAL * 2)  # now inside _slow_post
    assert not posted

    drained = asyncio.ensure_future(refresher.drain(GUILD))
    await asyncio.sleep(0)
    assert not drained.done(), "drain returned while a render was still in flight"

    gate.set()
    await drained
    assert posted == ["recorded the id"]
    refresher.cancel_all()


@pytest.mark.asyncio
async def test_two_renders_for_one_guild_do_not_interleave():
    """A slow inline render could otherwise land *after* the coalesced one and
    overwrite it, leaving the card naming the track before last."""
    order: list[str] = []
    refresher = CardRefresher(interval=INTERVAL)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow() -> None:
        order.append("slow-start")
        started.set()
        await release.wait()
        order.append("slow-end")

    async def _quick() -> None:
        order.append("quick")

    inline = asyncio.ensure_future(refresher.submit(GUILD, _slow))
    await started.wait()
    # A refresh arriving mid-render is queued and flushed on the timer — but it
    # still has to wait for the lock, so it cannot start yet.
    await refresher.submit(GUILD, _quick)
    await asyncio.sleep(INTERVAL * 3)
    assert order == ["slow-start"], "the quick render jumped the lock"

    release.set()
    await inline
    await asyncio.sleep(INTERVAL * 3)
    # The newest state still lands last, which is the point of coalescing.
    assert order == ["slow-start", "slow-end", "quick"]
    refresher.cancel_all()
