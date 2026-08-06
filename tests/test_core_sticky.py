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
    clear_placed_registry,
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
    # _channel resolves through get_channel_or_thread so a panel can opt into
    # threads (see target_types); get_channel is kept for the older assertions.
    guild.get_channel.return_value = channel
    guild.get_channel_or_thread.return_value = channel
    return guild


@pytest.fixture(autouse=True)
def _forget_placements():
    """The placed-message registry is module-level (shared across panels, which
    is the point — see was_placed). Tests reuse message ids, so clear it."""
    clear_placed_registry()
    yield
    clear_placed_registry()


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
    treated as postable — for a panel that did not opt into those kinds.

    Resolution goes through ``get_channel_or_thread``; setting only
    ``get_channel`` would leave this passing for the wrong reason (an unspec'd
    auto-mock fails the isinstance check whatever the panel accepts).
    """
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    voice = MagicMock(spec=discord.VoiceChannel)
    guild.get_channel.return_value = voice
    guild.get_channel_or_thread.return_value = voice
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


@pytest.mark.asyncio
async def test_a_burst_of_foreign_bot_posts_costs_one_repost():
    """The bounty board's shape end-to-end: many *other* bot messages, fast.

    Every other restick_on_bot test interleaves the panel's *own* repost. This
    one interleaves a burst of unrelated bot posts — new bounty cards landing in
    the hub's own channel — which is what the economy cog turned the flag on
    for. Ten cards inside one debounce window must leave one repost and a
    correctly recorded id.

    Deliberately an integration check, not a regression test for one guard:
    three independent layers each suffice here (debounce coalescing, the
    last_message_id pre-check, and only_if_buried under the placement lock), so
    removing any single one of them still passes. Verified by hand against all
    three. The narrow regressions live in the two tests that bracket this one —
    the shield in ``test_a_burst_landing_during_a_placement_still_settles`` and
    the coalescing in ``test_restick_debounce_collapses_a_burst``.
    """
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, 111), restick_on_bot=True, delay=0.02)

    async def _send(*_args, **_kwargs):
        channel.last_message_id = MESSAGE
        return sent

    channel.send = AsyncMock(side_effect=_send)

    for card_id in range(2000, 2010):
        channel.last_message_id = card_id  # each card takes the bottom slot
        await panel.on_message(_message(bot=True, message_id=card_id))
    await asyncio.sleep(0.1)

    assert channel.send.await_count == 1
    # And the hub really ended up below the cards, with its new id recorded.
    assert panel._ref[GUILD][1:] == (CHANNEL, MESSAGE)


@pytest.mark.asyncio
async def test_a_burst_landing_during_a_placement_still_settles():
    """The nastier burst: cards keep arriving while the repost is in flight.

    Each new card arms a restick, and that cancel lands on the task parked in
    place(). The placement is shielded so it still records its id, and the
    follow-up restick must then find the panel at the bottom and stop — leaving
    the panel posted exactly once per quiet period rather than climbing.
    """
    channel, sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, 111)
    panel = _panel(_bot(guild), store, restick_on_bot=True, delay=0.01)
    later = iter(range(3000, 3005))

    async def _send(*_args, **_kwargs):
        # Another bounty card lands mid-send, arming a restick whose cancel
        # hits the task currently inside this placement.
        card_id = next(later, None)
        if card_id is not None:
            channel.last_message_id = card_id
            await panel.on_message(_message(bot=True, message_id=card_id))
        await asyncio.sleep(0)
        channel.last_message_id = MESSAGE
        return sent

    channel.send = AsyncMock(side_effect=_send)

    channel.last_message_id = 2999
    await panel.on_message(_message(bot=True, message_id=2999))
    await asyncio.sleep(0.2)
    panel.cancel_all()

    assert store.ids == (CHANNEL, MESSAGE)  # recorded, not frozen on a dead id
    assert channel.send.await_count <= 2  # settled, never a per-card climb


@pytest.mark.asyncio
async def test_a_panel_posted_outside_place_is_invisible_until_forget():
    """The trap for any caller that posts its panel without going through
    ``place``.

    ``on_message`` reads ids through the TTL cache, and it caches "no panel"
    just as readily as a real id — for ``cache_ttl`` seconds, populated by
    *any* member message anywhere in the guild. So a feature that posts its
    first panel with a bare ``channel.send`` (rather than ``place`` /
    ``place_or_refresh``, which call ``_remember``) stays un-sticky until that
    entry lapses: up to five minutes of the panel simply not working.

    ``forget`` is the escape hatch, and a caller in that shape must use it.
    Regression for the economy auction card, which posts its own card at
    ``/bank auction start`` (2026-07-28).
    """
    store = _Store()  # nothing posted yet
    panel = _panel(_bot(), store)

    # Ordinary chat while no panel exists caches (0, 0).
    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message())
    sched.assert_not_called()

    # The feature now posts its panel itself and records the ids.
    store.ids = (CHANNEL, MESSAGE)

    # The cache still says "no panel", so the restick never arms.
    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message())
    sched.assert_not_called()

    panel.forget(GUILD)
    with patch.object(panel, "schedule_restick") as sched:
        await panel.on_message(_message())
    sched.assert_called_once_with(GUILD)


# ── two panels in one channel ─────────────────────────────────────────
#
# Discord has one bottom slot per channel. Everything above tests a single
# panel; these cover what the nine callers do to *each other*, which is where
# the 2026-08-06 review found the one High.


class _LiveChannel:
    """A channel that behaves like the gateway does.

    ``send`` assigns an increasing id, advances ``last_message_id`` and
    dispatches MESSAGE_CREATE to every listener *as a task* — i.e. concurrently
    with ``send`` returning, which is the ordering ``place`` documents and the
    one the July 2026 storm depended on. Faking it any other way makes the
    self-chase guards look stronger than they are.
    """

    def __init__(self):
        self.mock = MagicMock(spec=discord.TextChannel)
        self.mock.id = CHANNEL
        self.mock.last_message_id = None
        self.mock.send = self._send
        self.mock.get_partial_message = self._partial
        self._next = 1000
        self.sends = 0
        self.deletes = 0
        self.listeners: list[StickyPanel] = []

    async def _send(self, **_kwargs):
        await asyncio.sleep(0)  # the HTTP round trip
        self._next += 1
        self.sends += 1
        self.mock.last_message_id = self._next
        message = MagicMock(spec=discord.Message)
        message.id = self._next
        for panel in self.listeners:
            asyncio.create_task(panel.on_message(self._event(self._next, bot=True)))
        return message

    def _partial(self, message_id: int):
        outer = self

        class _Partial:
            id = message_id

            async def delete(self) -> None:
                outer.deletes += 1

            async def edit(self, **_kwargs) -> None:
                pass

        return _Partial()

    def _event(self, message_id: int, *, bot: bool):
        message = MagicMock(spec=discord.Message)
        message.id = message_id
        message.guild = MagicMock(spec=discord.Guild)
        message.guild.id = GUILD
        message.author = MagicMock(bot=bot)
        message.channel = MagicMock(id=CHANNEL)
        return message

    async def member_says(self) -> None:
        self._next += 1
        self.mock.last_message_id = self._next
        for panel in self.listeners:
            await panel.on_message(self._event(self._next, bot=False))

    def bot_says(self) -> int:
        """A bot message no panel placed — a casino round result."""
        self._next += 1
        self.mock.last_message_id = self._next
        return self._next


def _live_bot(live: _LiveChannel):
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel.return_value = live.mock
    guild.get_channel_or_thread.return_value = live.mock
    bot = MagicMock()
    bot.get_guild.return_value = guild
    return bot


@pytest.mark.asyncio
async def test_two_restick_on_bot_panels_in_one_channel_settle():
    """The 2026-08-06 High: a repost storm needing no human at all.

    Each panel's three self-chase guards only recognise its OWN message, so
    before ``was_placed`` panel A chased B's repost, which buried B, which
    chased A's, forever — and neither is ever at the bottom when its own
    debounce fires, so the at-the-bottom guard never engages. Measured then:
    26 sends across 40 debounce periods with nobody typing (~6.5/min in prod
    terms, indefinitely). Reachable from config alone — the casino hub and the
    bounty board hub are both opted in, and a live guild had both pointed at
    one channel.
    """
    live = _LiveChannel()
    bot = _live_bot(live)
    a = _panel(bot, _Store(CHANNEL, 1), restick_on_bot=True, delay=0.02)
    b = _panel(bot, _Store(CHANNEL, 2), restick_on_bot=True, delay=0.02)
    live.mock.last_message_id = 2
    live.listeners = [a, b]

    await live.member_says()  # one message, then silence
    await asyncio.sleep(0.02 * 40)
    a.cancel_all()
    b.cancel_all()

    # One repost each for the two buried panels, then quiet.
    assert live.sends <= 4, f"repost storm: {live.sends} sends with nobody typing"


@pytest.mark.asyncio
async def test_an_opted_in_panel_still_chases_a_real_bot_post():
    """Guard on the fix above: ``was_placed`` must only skip *placements*.

    The casino turned ``restick_on_bot`` on so a round settling with nobody
    typing doesn't strand the hub above the result. Skipping bot messages
    wholesale would silently undo that.
    """
    live = _LiveChannel()
    bot = _live_bot(live)
    panel = _panel(bot, _Store(CHANNEL, 1), restick_on_bot=True, delay=0.02)
    live.mock.last_message_id = 1
    live.listeners = [panel]

    round_result = live.bot_says()
    await panel.on_message(live._event(round_result, bot=True))
    await asyncio.sleep(0.02 * 8)
    panel.cancel_all()

    assert live.sends == 1


# ── failure ceiling ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_placement_stops_being_retried_after_repeated_failures():
    """A channel the bot lost Send Messages in used to be retried forever: one
    doomed REST call and one warning per burst of chat, with nothing surfaced."""
    channel, _sent = _channel()
    guild = _guild(channel)
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), max_place_failures=3)

    for _ in range(3):
        assert await panel.place(guild, channel) is None
    assert channel.send.await_count == 3
    assert panel.failing_guilds() == {GUILD}

    # The restick no longer arms, so chat costs nothing.
    with patch.object(panel, "_delayed_restick") as delayed:
        panel.schedule_restick(GUILD)
    delayed.assert_not_called()


@pytest.mark.asyncio
async def test_a_successful_repost_clears_the_failure_pause():
    channel, sent = _channel()
    guild = _guild(channel)
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE), max_place_failures=2)
    for _ in range(2):
        await panel.place(guild, channel)
    assert panel.failing_guilds() == {GUILD}

    channel.send = AsyncMock(return_value=sent)
    assert await panel.place(guild, channel) is not None
    assert panel.failing_guilds() == set()


@pytest.mark.asyncio
async def test_an_operator_refresh_clears_the_failure_pause():
    """place_or_refresh edits in place when the panel is already there, so it
    never reaches the send that would clear the count."""
    channel, _sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, MESSAGE)
    panel = _panel(_bot(guild), store, max_place_failures=1)
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))
    await panel.place(guild, channel)
    assert panel.failing_guilds() == {GUILD}

    await panel.place_or_refresh(guild, channel)
    assert panel.failing_guilds() == set()


# ── the replaced panel's delete ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_transient_delete_failure_is_retried():
    """The old panel's delete used to be one bare ``pass``. A live orphan keeps
    working — persistent views route by custom_id — so it is worth retrying."""
    channel, sent = _channel()
    guild = _guild(channel)
    old = channel.get_partial_message.return_value
    old.delete = AsyncMock(
        side_effect=[discord.HTTPException(MagicMock(), "503"), None]
    )
    panel = _panel(_bot(guild), _Store(CHANNEL, 999))

    with patch("bot_modules.core.sticky.asyncio.sleep", new=AsyncMock()):
        assert await panel.place(guild, channel) is sent
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert old.delete.await_count == 2


@pytest.mark.asyncio
async def test_a_deleted_old_panel_is_not_retried():
    """NotFound is the ordinary case (a member removed it by hand)."""
    channel, sent = _channel()
    guild = _guild(channel)
    old = channel.get_partial_message.return_value
    old.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
    panel = _panel(_bot(guild), _Store(CHANNEL, 999))

    assert await panel.place(guild, channel) is sent
    assert old.delete.await_count == 1
    assert not panel._orphan_tasks


# ── channel deletion ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_channel_delete_clears_the_panel_ids():
    channel, _sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, MESSAGE)
    panel = _panel(_bot(guild), store)
    panel.set_known_guilds({GUILD})

    deleted = MagicMock(spec=discord.TextChannel)
    deleted.id = CHANNEL
    deleted.guild = guild
    await panel.on_channel_delete(deleted)

    assert store.ids == (0, 0)
    assert panel._known == set()


@pytest.mark.asyncio
async def test_on_channel_delete_ignores_some_other_channel():
    channel, _sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, MESSAGE)
    panel = _panel(_bot(guild), store)

    other = MagicMock(spec=discord.TextChannel)
    other.id = 4242
    other.guild = guild
    await panel.on_channel_delete(other)

    assert store.ids == (CHANNEL, MESSAGE)


# ── refresh: repost vs retire ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_can_retire_a_deleted_panel_instead_of_reposting():
    """What the economy leaderboard's hourly loop wants: deleting the message
    is how staff retire that panel, so a 404 must clear the ids rather than
    bring it back."""
    channel, _sent = _channel()
    guild = _guild(channel)
    store = _Store(CHANNEL, MESSAGE)
    panel = _panel(_bot(guild), store)
    channel.get_partial_message.return_value.edit = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), "gone")
    )

    assert await panel.refresh(GUILD, repost_if_missing=False) is False
    assert store.ids == (0, 0)
    assert channel.send.await_count == 0


# ── burial ceiling ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_burial_reposts_even_if_the_channel_never_falls_quiet():
    """The debounce is purely trailing-edge, so a conversation with no gap
    longer than ``delay`` leaves the panel buried for its whole duration.
    Off by default; this covers the opt-in ceiling."""
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(
        _bot(guild), _Store(CHANNEL, 111), delay=0.05, max_burial=0.05
    )

    async def _send(*_args, **_kwargs):
        channel.last_message_id = MESSAGE
        return sent

    channel.send = AsyncMock(side_effect=_send)

    # Chat that never pauses for the debounce: 12 messages at delay/4.
    for message_id in range(5000, 5012):
        channel.last_message_id = message_id
        await panel.on_message(_message(message_id=message_id))
        await asyncio.sleep(0.05 / 4)
    await asyncio.sleep(0.15)
    panel.cancel_all()

    assert channel.send.await_count >= 1


@pytest.mark.asyncio
async def test_without_max_burial_unbroken_chat_never_reposts():
    """The default stays uncapped — a timing change for every live panel is not
    something this pass gets to make silently."""
    channel, sent = _channel()
    guild = _guild(channel)
    panel = _panel(_bot(guild), _Store(CHANNEL, 111), delay=0.05)
    channel.send = AsyncMock(return_value=sent)

    for message_id in range(5000, 5012):
        channel.last_message_id = message_id
        await panel.on_message(_message(message_id=message_id))
        await asyncio.sleep(0.05 / 4)
    panel.cancel_all()

    assert channel.send.await_count == 0


# ── target types ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_thread_is_refused_by_default():
    """The auction card's channel warning relies on this: threads stay out of
    the default set, so widening it for one feature can't change another's."""
    thread = MagicMock(spec=discord.Thread)
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel_or_thread.return_value = thread
    panel = _panel(_bot(guild), _Store(CHANNEL, MESSAGE))
    assert await panel.refresh(GUILD) is False


@pytest.mark.asyncio
async def test_a_thread_is_accepted_when_the_panel_opts_in():
    """What the guess prompt needs — its channel may be a thread or the text
    view of a voice channel."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = CHANNEL
    edited = MagicMock()
    edited.edit = AsyncMock()
    thread.get_partial_message = MagicMock(return_value=edited)
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel_or_thread.return_value = thread
    panel = _panel(
        _bot(guild),
        _Store(CHANNEL, MESSAGE),
        target_types=(discord.TextChannel, discord.VoiceChannel, discord.Thread),
    )
    assert await panel.refresh(GUILD) is True
    edited.edit.assert_awaited_once()
