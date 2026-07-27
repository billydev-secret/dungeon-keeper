"""Tests for core/sticky.py — the shared channel-bottom panel machinery.

These pin the behaviours the four call sites depend on, several of which were
bugs in the copies this module replaced (delete-before-post losing a live
panel, `Forbidden`-only catches, no unchanged-content gate).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.sticky import (
    PanelContent,
    StickyPanel,
    should_restick,
)

GUILD = 123
CHANNEL = 555
MESSAGE = 666


def _channel(channel_id: int = CHANNEL, message_id: int = MESSAGE):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    # Gateway-maintained; "something else is below the panel" unless a test
    # says otherwise, so resticks are not skipped by the at-the-bottom guard.
    channel.last_message_id = None
    sent = MagicMock(spec=discord.Message)
    sent.id = message_id
    sent.edit = AsyncMock()
    sent.delete = AsyncMock()
    channel.get_partial_message = MagicMock(return_value=sent)
    channel.send = AsyncMock(return_value=sent)
    return channel, sent


def _guild(channel):
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel.return_value = channel
    return guild


class _Store:
    """Stand-in for a feature's id storage."""

    def __init__(self, channel_id: int = 0, message_id: int = 0):
        self.ids = (channel_id, message_id)
        self.saves: list[tuple[int, int, int]] = []

    def load(self, guild_id: int) -> tuple[int, int]:
        return self.ids

    def save(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self.ids = (channel_id, message_id)
        self.saves.append((guild_id, channel_id, message_id))


def _panel(bot, store, *, signature=None, build=None, **kw) -> StickyPanel:
    async def _build(guild):
        return PanelContent(embed=discord.Embed(title="p"), signature=signature)

    return StickyPanel(
        "test panel",
        bot,
        load_ids=store.load,
        save_ids=store.save,
        build=build or _build,
        **kw,
    )


def _bot(guild=None):
    bot = MagicMock()
    bot.get_guild.return_value = guild
    return bot


# ── the predicate ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("panel_channel", "panel_message", "msg_channel", "msg_id", "expected"),
    [
        (0, 0, CHANNEL, 1, False),          # nothing posted yet
        (CHANNEL, MESSAGE, 999, 1, False),  # activity elsewhere
        (CHANNEL, MESSAGE, CHANNEL, MESSAGE, False),  # the panel itself
        (CHANNEL, MESSAGE, CHANNEL, 1, True),         # a member posted below it
    ],
)
def test_should_restick(panel_channel, panel_message, msg_channel, msg_id, expected):
    assert should_restick(
        message_channel_id=msg_channel,
        message_id=msg_id,
        panel_channel_id=panel_channel,
        panel_message_id=panel_message,
    ) is expected


# ── placement ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_place_posts_and_persists():
    channel, sent = _channel()
    guild = _guild(channel)
    store = _Store()
    panel = _panel(_bot(guild), store)

    assert await panel.place(guild, channel) is sent
    channel.send.assert_awaited_once()
    assert store.ids == (CHANNEL, MESSAGE)


@pytest.mark.asyncio
async def test_place_removes_the_previous_panel():
    channel, sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, 111)
    panel = _panel(_bot(guild), store)

    await panel.place(guild, channel)
    channel.get_partial_message.assert_called_once_with(111)
    sent.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_place_posts_before_deleting():
    """Regression (economy panels): deleting first destroys a working panel
    whenever the new channel turns out to be unpostable."""
    channel, sent = _channel()
    order: list[str] = []
    channel.send = AsyncMock(side_effect=lambda **kw: (order.append("send"), sent)[1])
    sent.delete = AsyncMock(side_effect=lambda: order.append("delete"))
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, 111))

    await panel.place(guild, channel)
    assert order == ["send", "delete"]


@pytest.mark.parametrize(
    "error",
    [
        discord.Forbidden(MagicMock(status=403), "no perms"),
        discord.HTTPException(MagicMock(status=500), "boom"),
    ],
)
@pytest.mark.asyncio
async def test_failed_post_keeps_the_old_panel(error):
    """Regression: economy caught only Forbidden, so a 5xx escaped — and by
    then the old panel had already been deleted."""
    channel, sent = _channel()
    channel.send = AsyncMock(side_effect=error)
    guild = _guild(channel)
    store = _Store(CHANNEL, 111)
    panel = _panel(_bot(guild), store)

    assert await panel.place(guild, channel) is None
    sent.delete.assert_not_awaited()
    assert store.ids == (CHANNEL, 111)


@pytest.mark.asyncio
async def test_place_survives_a_failed_delete_of_the_old_panel():
    channel, sent = _channel()
    sent.delete = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "already gone")
    )
    guild = _guild(channel)
    store = _Store(CHANNEL, 111)
    panel = _panel(_bot(guild), store)

    assert await panel.place(guild, channel) is not None
    assert store.ids == (CHANNEL, MESSAGE)


@pytest.mark.asyncio
async def test_place_is_serialised_per_guild():
    """Two concurrent placements must not interleave into two live panels."""
    channel, sent = _channel()
    guild = _guild(channel)
    active = 0
    peak = 0

    async def _build(_guild):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return PanelContent(embed=discord.Embed(title="p"))

    panel = _panel(_bot(guild), _Store(), build=_build)
    await asyncio.gather(panel.place(guild, channel), panel.place(guild, channel))
    assert peak == 1


@pytest.mark.asyncio
async def test_unpost_deletes_and_clears():
    channel, sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, MESSAGE)
    panel = _panel(_bot(guild), store)

    assert await panel.unpost(guild) is True
    sent.delete.assert_awaited_once()
    assert store.ids == (0, 0)


@pytest.mark.asyncio
async def test_unpost_without_a_panel_reports_false():
    channel, _ = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store())
    assert await panel.unpost(guild) is False


# ── refresh ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_edits_in_place():
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE))

    assert await panel.refresh(GUILD) is True
    sent.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_noop_without_a_panel():
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store())
    assert await panel.refresh(GUILD) is False
    sent.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_skips_the_api_call_when_unchanged():
    """The whole point of the signature gate — ages tick client-side."""
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), signature=("a",))

    assert await panel.refresh(GUILD) is True
    assert await panel.refresh(GUILD) is False
    assert sent.edit.await_count == 1


@pytest.mark.asyncio
async def test_refresh_always_edits_without_a_signature():
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), signature=None)

    await panel.refresh(GUILD)
    await panel.refresh(GUILD)
    assert sent.edit.await_count == 2


@pytest.mark.asyncio
async def test_refresh_reposts_when_the_panel_was_deleted():
    channel, sent = _channel()
    sent.edit = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    guild = _guild(channel)
    store = _Store(CHANNEL, MESSAGE)
    panel = _panel(_bot(guild), store)

    assert await panel.refresh(GUILD) is True
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_edit_is_queued_for_retry():
    channel, sent = _channel()
    sent.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), signature=("a",))

    assert await panel.refresh(GUILD) is False
    assert panel.take_retries() == {GUILD}
    assert panel.take_retries() == set()  # drained
    # The signature must stay stale, or the retry would decide nothing changed.
    assert await panel.refresh(GUILD) is False


# ── sticky behaviour ──────────────────────────────────────────────────


def _message(*, bot: bool = False, channel_id: int = CHANNEL, message_id: int = 1):
    msg = MagicMock(spec=discord.Message)
    msg.guild = MagicMock(id=GUILD)
    msg.author = MagicMock(bot=bot)
    msg.channel = MagicMock(id=channel_id)
    msg.id = message_id
    return msg


@pytest.mark.asyncio
async def test_on_message_arms_a_restick():
    panel = _panel(_bot(), _Store(CHANNEL, MESSAGE))
    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message())
    sched.assert_called_once_with(GUILD)


@pytest.mark.asyncio
async def test_on_message_ignores_bots():
    """Re-sticking under our own repost would self-loop forever."""
    panel = _panel(_bot(), _Store(CHANNEL, MESSAGE))
    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message(bot=True))
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_ignores_other_channels():
    panel = _panel(_bot(), _Store(CHANNEL, MESSAGE))
    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message(channel_id=999))
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_known_guilds_fast_path_avoids_the_id_load():
    """The listener runs for every message in every guild; a guild with no
    panel must not pay a DB read to re-learn that."""
    store = _Store()
    store.load = MagicMock(side_effect=AssertionError("should not be consulted"))
    panel = _panel(_bot(), store)
    panel.set_known_guilds(set())

    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message())
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_ids_are_cached_between_messages():
    store = _Store(CHANNEL, MESSAGE)
    store.load = MagicMock(return_value=(CHANNEL, MESSAGE))
    panel = _panel(_bot(), store)

    with patch.object(panel, "schedule_restick"):
        await panel.on_message(_message(message_id=1))
        await panel.on_message(_message(message_id=2))
    assert store.load.call_count == 1


@pytest.mark.asyncio
async def test_restick_debounce_collapses_a_burst():
    """A burst of chat must cost one repost, not one per message."""
    channel, _ = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), delay=0.01)
    with patch.object(panel, "place", new=AsyncMock()) as place:
        for _ in range(5):
            panel.schedule_restick(GUILD)
        await asyncio.sleep(0.05)
    assert place.await_count == 1


@pytest.mark.asyncio
async def test_restick_never_creates_a_panel_that_was_never_posted():
    channel, _ = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(), delay=0.01)
    with patch.object(panel, "place", new=AsyncMock()) as place:
        panel.schedule_restick(GUILD)
        await asyncio.sleep(0.05)
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_all_stops_pending_resticks():
    channel, _ = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), delay=0.05)
    with patch.object(panel, "place", new=AsyncMock()) as place:
        panel.schedule_restick(GUILD)
        panel.cancel_all()
        await asyncio.sleep(0.1)
    place.assert_not_awaited()


# ── channel resolution ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_text_channels_are_refused():
    """A stored id that now points at a voice channel or thread must not be
    treated as postable."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel.return_value = MagicMock(spec=discord.VoiceChannel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE))
    assert await panel.refresh(GUILD) is False


# ── hold hook ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hold_defers_the_restick_until_it_clears():
    """Casino's case: don't move the panel out from under a live round."""
    channel, _ = _channel()
    guild = _guild(channel)
    held = {"value": True}

    async def _hold(_gid):
        return held["value"]

    panel = _panel(
        _bot(guild), _Store(CHANNEL, MESSAGE),
        delay=0.01, hold=_hold, hold_poll=0.01,
    )
    with patch.object(panel, "place", new=AsyncMock()) as place:
        panel.schedule_restick(GUILD)
        await asyncio.sleep(0.05)
        place.assert_not_awaited()  # still held
        held["value"] = False
        await asyncio.sleep(0.05)
    place.assert_awaited_once()


@pytest.mark.asyncio
async def test_hold_gives_up_after_the_ceiling():
    """A hold that never clears must not bury the panel forever."""
    channel, _ = _channel()
    guild = _guild(channel)

    async def _hold(_gid):
        return True

    panel = _panel(
        _bot(guild), _Store(CHANNEL, MESSAGE),
        delay=0.01, hold=_hold, hold_poll=0.01, hold_max=0.03,
    )
    with patch.object(panel, "place", new=AsyncMock()) as place:
        panel.schedule_restick(GUILD)
        await asyncio.sleep(0.2)
    place.assert_awaited_once()


@pytest.mark.asyncio
async def test_hold_does_not_gate_an_explicit_place():
    """An admin reposting deliberately shouldn't wait on a live round."""
    channel, sent = _channel()
    guild = _guild(channel)

    async def _hold(_gid):
        return True

    panel = _panel(_bot(guild), _Store(), hold=_hold)
    assert await panel.place(guild, channel) is sent


@pytest.mark.asyncio
async def test_no_hold_hook_proceeds_immediately():
    channel, _ = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), delay=0.01)
    with patch.object(panel, "place", new=AsyncMock()) as place:
        panel.schedule_restick(GUILD)
        await asyncio.sleep(0.05)
    place.assert_awaited_once()


# ── restick_on_bot ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_messages_arm_a_restick_when_opted_in():
    """Regression: the casino buries its own panel with round results, so
    filtering bot authors left the hub stranded above them with nobody typing."""
    panel = _panel(_bot(), _Store(CHANNEL, MESSAGE), restick_on_bot=True)
    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message(bot=True))
    sched.assert_called_once_with(GUILD)


@pytest.mark.asyncio
async def test_opted_in_panel_still_skips_its_own_message():
    """Cheap skip when the id is already cached — an optimisation, not the
    self-loop protection (that races the gateway; see the two tests below)."""
    panel = _panel(_bot(), _Store(CHANNEL, MESSAGE), restick_on_bot=True)
    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message(bot=True, message_id=MESSAGE))
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_restick_is_skipped_when_the_panel_is_already_last():
    """Nothing is buried, so there is nothing to move — and no API call."""
    channel, _ = _channel()
    channel.last_message_id = MESSAGE
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), delay=0.01)
    with patch.object(panel, "place", new=AsyncMock()) as place:
        panel.schedule_restick(GUILD)
        await asyncio.sleep(0.05)
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_repost_does_not_self_loop_when_the_gateway_wins():
    """Prod repro: the casino hub reposted itself every ~6s in bursts.

    The MESSAGE_CREATE frame for our own repost is dispatched while place() is
    still awaiting send(), so _remember() has not run yet and the id cache
    still holds the *old* panel — should_restick() waves the repost through and
    arms a restick. That restick must find the panel already at the bottom and
    do nothing; otherwise each repost arms the next one forever.
    """
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, 111), restick_on_bot=True, delay=0.01)

    async def _send(*args, **kwargs):
        channel.last_message_id = MESSAGE  # discord.py updates this first…
        await panel.on_message(_message(bot=True, message_id=MESSAGE))  # …then dispatches
        return sent

    channel.send = AsyncMock(side_effect=_send)

    await panel.place(guild, channel)
    await asyncio.sleep(0.05)  # let the armed restick fire

    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_new_id_is_cached_before_the_old_panel_is_deleted():
    """The gateway event for our own repost can land during the delete await;
    the id must already be recorded or restick_on_bot self-loops."""
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, 111), restick_on_bot=True)
    seen: list[tuple[int, int]] = []

    async def _delete():
        # Stand in for the gateway event arriving mid-delete.
        seen.append(await panel._cached_ids(GUILD))

    sent.delete = AsyncMock(side_effect=_delete)
    await panel.place(guild, channel)
    assert seen == [(CHANNEL, MESSAGE)]


@pytest.mark.asyncio
async def test_a_cancelled_debounce_cannot_abandon_a_placement():
    """Prod repro (casino hub, 2026-07-26): one panel every 6s for hours,
    surviving a restart.

    schedule_restick() cancel-and-rearms, and the task it cancels is the one
    running place(), parked in send(). Discord has taken the message by then,
    so the panel posts — but _remember(), the old-panel delete and the id save
    never run. The stored id stays frozen on a dead message, so the next
    restick's at-the-bottom guard compares against *that*, sees a mismatch, and
    posts again: a loop nothing downstream can break, because every iteration
    destroys the evidence the guard needs.
    """
    channel, sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, 111)
    panel = _panel(_bot(guild), store, restick_on_bot=True, delay=0.01)

    async def _send(*_args, **_kwargs):
        # discord.py sets last_message_id and dispatches MESSAGE_CREATE while
        # we are still awaiting the HTTP response…
        channel.last_message_id = MESSAGE
        await panel.on_message(_message(bot=True, message_id=MESSAGE))
        # …so the restick that event arms lands its cancel here, mid-send.
        await asyncio.sleep(0)
        return sent

    channel.send = AsyncMock(side_effect=_send)

    panel.schedule_restick(GUILD)
    await asyncio.sleep(0.1)
    panel.cancel_all()

    assert store.ids == (CHANNEL, MESSAGE)  # recorded despite the cancel
    sent.delete.assert_awaited_once()  # and the old panel really went
    assert channel.send.await_count == 1  # no runaway repost


@pytest.mark.asyncio
async def test_a_queued_restick_sees_the_placement_it_waited_on():
    """Two resticks in flight must not stack two panels.

    The second passed the cached-id pre-check before the first placement
    finished, then blocked on the per-guild lock. Re-deciding under the lock —
    against ids read there — is what turns it into a no-op.
    """
    channel, sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, 111)
    panel = _panel(_bot(guild), store)

    async def _send(*_args, **_kwargs):
        channel.last_message_id = MESSAGE
        return sent

    channel.send = AsyncMock(side_effect=_send)

    await asyncio.gather(
        panel.place(guild, channel, only_if_buried=True),
        panel.place(guild, channel, only_if_buried=True),
    )
    assert channel.send.await_count == 1
