"""Panels that stay pinned to the bottom of a channel.

Discord has no reorder API, so a "sticky" panel keeps its position by being
deleted and re-posted whenever a member posts beneath it. Several features want
that, and until this module existed each one carried its own copy of the
machinery — per-guild lock map, debounce-task map, id cache, ``on_message``
listener, delete-and-repost placer.

The copies drifted. ``StickyPanel`` below is the todo board's version, which was
the best of them, so every caller now gets:

* **post-before-delete** — a working panel survives a move into a channel the
  bot can't post in, instead of being destroyed before the failure is known;
* **``HTTPException`` not just ``Forbidden``** — a rate-limit or an oversized
  embed no longer escapes as an unhandled error;
* **one REST call** via ``get_partial_message`` instead of ``fetch_message``
  plus edit/delete;
* an optional **unchanged-content gate**, so a refresh that would render the
  same panel costs no API call at all;
* a **guild fast-path**, so the ``on_message`` listener rejects guilds with no
  panel without touching the database;
* **placements atomic to cancellation**, so the debounce's cancel-and-rearm can
  never leave a posted panel unrecorded (see ``place``);
* **no panel chases another panel's repost** — a panel whose bottom slot was
  taken by *another* sticky panel yields instead of taking it back, which is what
  stops two panels sharing a channel from re-posting each other forever (see
  ``was_placed``);
* a **failure ceiling**, so a channel the bot cannot post in stops being
  retried on every burst of chat forever (see ``failing_guilds``).

See ``docs/plans/sticky-panel-extraction.md`` for the full site survey,
including the panels that are deliberately *not* built on this.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Hashable, cast

import discord

log = logging.getLogger(__name__)

#: Channel kinds a panel can live in. ``TextChannel`` is the default because
#: most callers only ever want one; ``guess`` opts into the wider set via
#: ``target_types``, and the auction card's channel warning deliberately relies
#: on threads staying *out* of the default (see ``_sticky_check`` in
#: ``economy/auction_views.py``).
StickyTarget = discord.TextChannel | discord.VoiceChannel | discord.Thread

#: How long a panel's ``(channel_id, message_id)`` stays cached. The listener
#: runs for every message in every guild, so this keeps it a dict lookup.
DEFAULT_CACHE_TTL = 300.0

#: Quiet period before re-sticking. A burst of chat costs one repost, not one
#: per message.
DEFAULT_DELAY = 6.0

#: How often to re-check a ``hold`` predicate, and how long to keep waiting on
#: one before giving up and re-sticking anyway. Without the ceiling a
#: never-clearing hold would strand the panel at the top of the channel forever.
DEFAULT_HOLD_POLL = 15.0
DEFAULT_HOLD_MAX = 600.0

#: Consecutive failed placements before a panel stops arming resticks. A channel
#: the bot has lost Send Messages in would otherwise cost one doomed REST call
#: and one warning line per burst of chat, forever, with nothing surfaced to the
#: admin. An explicit ``place``/``place_or_refresh`` always retries and clears
#: the count, so re-posting from the dashboard is the recovery.
DEFAULT_MAX_PLACE_FAILURES = 5

#: How many placed message ids to remember, across every panel in the process.
#: Only has to outlive the longest debounce by enough to cover a repost, so this
#: is orders of magnitude more than needed.
_PLACED_HISTORY = 512

# Message ids that some StickyPanel posted. Shared across panels deliberately: a
# panel must recognise *any* sticky panel's placement, not just its own. Two
# panels opted into ``restick_on_bot`` in one channel would otherwise re-post
# each other forever — each one's self-chase guards only know its own message id,
# and neither is ever at the bottom when its own debounce fires, so the
# at-the-bottom guard never engages. Measured before this existed: ~6.5 reposts a
# minute in one channel, indefinitely, with nobody typing.
_placed_order: deque[int] = deque()
_placed: set[int] = set()


def _note_placed(message_id: int) -> None:
    """Record a message some panel just posted, evicting the oldest."""
    if message_id in _placed:
        return
    if len(_placed_order) >= _PLACED_HISTORY:
        _placed.discard(_placed_order.popleft())
    _placed_order.append(message_id)
    _placed.add(message_id)


def was_placed(message_id: int) -> bool:
    """Whether this message is a sticky panel's own placement.

    Consulted in two places, and only one of them decides:

    * ``on_message`` — an **optimisation**, and an unreliable one. ``_note_placed``
      runs inside ``_remember``, i.e. after ``send()`` returns, so this races the
      gateway exactly as ``should_restick``'s id-skip does, and any yield between
      the frame being dispatched and ``_remember`` running loses the race — which
      the awaited HTTP response guarantees. It saves a doomed debounce when it
      wins; it must never be the protection.
    * ``_delayed_restick`` / ``_place_locked`` — where it **decides**. Those run a
      whole debounce later, and under the placement lock, so the registry is
      reliably populated by then.

    Measured with only the ``on_message`` check: two opted-in panels in one
    channel still storm (~29 sends across 40 debounce periods).
    """
    return message_id in _placed


def clear_placed_registry() -> None:
    """Forget every recorded placement. For tests, which reuse message ids."""
    _placed_order.clear()
    _placed.clear()


def should_restick(
    *,
    message_channel_id: int,
    message_id: int,
    panel_channel_id: int,
    panel_message_id: int,
) -> bool:
    """Whether a new message should push a sticky panel back to the bottom.

    Bot messages are filtered out by the caller unless ``restick_on_bot`` is
    set, so this predicate usually only sees member activity. The message-id
    guard below skips the panel's own message when the id is already cached —
    but that is a race against the gateway, so it is an optimisation, not the
    self-loop protection. The decided check is the already-at-the-bottom test
    in ``_place_locked``, taken under the placement lock.
    """
    if not panel_channel_id or not panel_message_id:
        return False  # no panel posted yet
    if message_channel_id != panel_channel_id:
        return False  # activity in some other channel
    return message_id != panel_message_id  # skip our own panel


@dataclass(frozen=True)
class PanelContent:
    """What a panel should currently look like.

    ``signature`` is an optional fingerprint of the *rendered* content. When a
    caller supplies one, ``refresh`` compares it against the last edit and skips
    the API call when nothing changed — exclude anything that renders
    client-side (``<t:…:R>`` ages tick on their own) so an unchanged panel stays
    unchanged.
    """

    embed: discord.Embed
    #: discord.py distinguishes "no view" (MISSING) from "clear the view"
    #: (None), and its send/edit overloads reject a bare None.
    view: discord.ui.View = discord.utils.MISSING
    signature: Hashable | None = None


class StickyPanel:
    """One feature's channel-bottom panel, managed across every guild.

    The owning cog supplies three callbacks and forwards two events:

    * ``load_ids(guild_id) -> (channel_id, message_id)`` — **sync**, run in a
      worker thread. ``(0, 0)`` means "not posted".
    * ``save_ids(guild_id, channel_id, message_id)`` — **sync**, run in a
      worker thread. Called with ``(0, 0)`` on unpost.
    * ``build(guild) -> PanelContent`` — **async**, called under the per-guild
      lock so it can read whatever it needs consistently.

    Optionally also:

    * ``hold(guild_id) -> bool`` — "not yet". While it returns True the restick
      waits, re-checking every ``hold_poll`` seconds up to ``hold_max``. Use it
      when moving the panel would disrupt something in flight (a live betting
      round, say). It gates the *sticky repost* only; an explicit ``place`` is
      always honoured.
    * ``restick_on_bot`` — also re-stick under *bot* messages. Off by default,
      because chasing our own notices is churn and re-sticking under our own
      repost self-loops. Turn it on where the bot is the main thing burying the
      panel (the casino posts round results into its own hub channel). A panel
      that is already the channel's last message is never re-sticked, a
      placement always records the panel it posted (see ``place``), and no panel
      chases a message another panel placed (see ``was_placed``), so this
      cannot loop — against itself or against a second opted-in panel.
    * ``target_types`` — which channel kinds the panel may live in. Defaults to
      ``TextChannel`` alone; pass a wider tuple for a feature whose channel can
      be a thread or a voice channel's text view.
    * ``max_burial`` — re-stick anyway once the panel has been buried this long,
      even if the channel never falls quiet. The debounce is purely
      trailing-edge, so without this a conversation that never pauses for
      ``delay`` seconds leaves the panel buried for its whole duration. ``None``
      (the default) keeps that uncapped behaviour.

    Then, from the cog: ``on_message`` from a listener, ``on_channel_delete``
    from another, and ``cancel_all()`` from ``cog_unload``.
    """

    def __init__(
        self,
        name: str,
        bot: discord.Client,
        *,
        load_ids: Callable[[int], tuple[int, int]],
        save_ids: Callable[[int, int, int], None],
        build: Callable[[discord.Guild], Awaitable[PanelContent]],
        hold: Callable[[int], Awaitable[bool]] | None = None,
        restick_on_bot: bool = False,
        hold_poll: float = DEFAULT_HOLD_POLL,
        hold_max: float = DEFAULT_HOLD_MAX,
        delay: float = DEFAULT_DELAY,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        target_types: tuple[type, ...] = (discord.TextChannel,),
        max_burial: float | None = None,
        max_place_failures: int = DEFAULT_MAX_PLACE_FAILURES,
    ) -> None:
        self.name = name
        self.bot = bot
        self._load_ids = load_ids
        self._save_ids = save_ids
        self._build = build
        self._hold = hold
        self._restick_on_bot = restick_on_bot
        self._hold_poll = hold_poll
        self._hold_max = hold_max
        self._delay = delay
        self._cache_ttl = cache_ttl
        self._target_types = target_types
        self._max_burial = max_burial
        self._max_place_failures = max_place_failures

        # defaultdict, not setdefault: the latter builds a throwaway Lock on
        # every call just to discard it.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._restick_tasks: dict[int, asyncio.Task[None]] = {}
        # guild → (monotonic expiry, channel_id, message_id)
        self._ref: dict[int, tuple[float, int, int]] = {}
        self._signatures: dict[int, Hashable] = {}
        # Guilds known to have a panel. Optional: when a caller publishes this
        # (see set_known_guilds) the listener rejects everything else without
        # a DB read. Until then the TTL cache carries the load.
        self._known: set[int] | None = None
        # Guilds whose in-place edit failed; drained via take_retries().
        self._retry: set[int] = set()
        # guild → consecutive failed placements, and when its panel was first
        # buried in the current run of activity.
        self._failures: dict[int, int] = {}
        self._buried_since: dict[int, float] = {}
        # Detached best-effort retries of an old panel's delete. Held so they
        # are cancellable and so the tasks are not garbage-collected mid-flight.
        self._orphan_tasks: set[asyncio.Task[None]] = set()

    # ── state the owning cog can publish ─────────────────────────────────

    def take_retries(self) -> set[int]:
        """Guilds whose last in-place edit failed, cleared as they're returned.

        A caller with a periodic loop drains this so a transient Discord error
        doesn't strand a stale panel until the next mutation. Callers without
        such a loop simply never call it.
        """
        pending, self._retry = self._retry, set()
        return pending

    def failing_guilds(self) -> set[int]:
        """Guilds where placement has failed enough times to have given up.

        Read-only, unlike ``take_retries`` — this is state a dashboard should be
        able to show repeatedly ("the bot can't post this panel"), not a queue.
        Cleared by a successful placement or by ``forget``.
        """
        return {
            gid
            for gid, count in self._failures.items()
            if count >= self._max_place_failures
        }

    def set_known_guilds(self, guild_ids: set[int]) -> None:
        """Tell the listener which guilds actually have a panel.

        Optional. Without it every guild pays a cached DB read every
        ``cache_ttl`` seconds, forever, to re-learn it has no panel.
        """
        self._known = set(guild_ids)

    # ── reads ────────────────────────────────────────────────────────────

    async def ids(self, guild_id: int) -> tuple[int, int]:
        return await asyncio.to_thread(self._load_ids, guild_id)

    async def _cached_ids(self, guild_id: int) -> tuple[int, int]:
        entry = self._ref.get(guild_id)
        now = time.monotonic()
        if entry is not None and entry[0] > now:
            return entry[1], entry[2]
        channel_id, message_id = await self.ids(guild_id)
        self._ref[guild_id] = (now + self._cache_ttl, channel_id, message_id)
        return channel_id, message_id

    def _channel(
        self, guild: discord.Guild, channel_id: int
    ) -> StickyTarget | None:
        """Resolve a stored id to a postable channel in *this* guild.

        Scoped to the guild deliberately: panel ids are dashboard-supplied, and
        a cross-guild lookup would let one server's config point at another's
        channel. Which kinds count is per-panel (``target_types``), so widening
        it for one feature cannot quietly change another's behaviour.
        """
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        if channel is None or not isinstance(channel, self._target_types):
            return None
        return cast(StickyTarget, channel)

    # ── placement ────────────────────────────────────────────────────────

    async def place(
        self,
        guild: discord.Guild,
        target: StickyTarget,
        *,
        only_if_buried: bool = False,
    ) -> discord.Message | None:
        """Post a fresh panel at the bottom of ``target``, remove the old one,
        and persist the new ids.

        Returns None when posting fails, leaving any existing panel untouched.
        Serialised per guild, so a manual post and a sticky repost can't race
        into two panels.

        ``only_if_buried`` — what the sticky repost wants: do nothing (and
        return None) if the stored panel is already the last message in
        ``target``. An explicit post never passes it.

        Cancelling the caller cannot abandon a placement half-done. The panel
        is posted before it is recorded, so a cancel landing in between would
        leave a live panel whose id nobody holds: the previous panel never
        deleted and the stored id frozen on a dead message. ``schedule_restick``
        cancel-and-rearms, and the task it cancels may be exactly the one parked
        in ``send()`` here — for a ``restick_on_bot`` panel that is a loop,
        since the repost's own gateway event arms the restick that cancels the
        placement which would have recorded it. (Prod, 2026-07-26: the casino
        hub reposted every 6s for hours and survived a restart, because the
        stored id never advanced past a long-dead message.) So the placement
        runs as a shielded task: the caller's cancellation stops the *waiting*,
        never the work.
        """
        task = asyncio.ensure_future(
            self._place_locked(guild, target, only_if_buried=only_if_buried)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # We're going away; the placement finishes on its own. Keep hold of
            # its outcome so a failure is logged rather than surfacing as an
            # unretrieved-exception warning whenever the task is collected.
            task.add_done_callback(self._log_abandoned_placement)
            raise

    def _log_abandoned_placement(
        self, task: asyncio.Task[discord.Message | None]
    ) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error(
                "%s: placement failed after its caller was cancelled: %r",
                self.name,
                task.exception(),
            )

    @staticmethod
    def _at_bottom(
        target: StickyTarget, channel_id: int, message_id: int
    ) -> bool:
        """Whether the stored panel is already the last message in ``target``.

        Free — ``last_message_id`` is gateway-maintained, not an API call.
        Callers take this under the placement lock against a freshly read id,
        so a restick that queued behind another placement sees *that* one's
        result instead of posting a second panel on top of it.
        """
        return bool(
            message_id
            and channel_id == target.id
            and target.last_message_id == message_id
        )

    async def _place_locked(
        self,
        guild: discord.Guild,
        target: StickyTarget,
        *,
        only_if_buried: bool = False,
    ) -> discord.Message | None:
        async with self._locks[guild.id]:
            # Re-read the stored ids INSIDE the lock — a caller's pre-lock
            # snapshot can be stale after a racing post, and deleting it would
            # orphan the live panel.
            old_channel_id, old_message_id = await self.ids(guild.id)
            if only_if_buried and (
                self._at_bottom(target, old_channel_id, old_message_id)
                # Re-decided under the lock, so a restick that queued behind
                # another panel's placement sees that placement rather than
                # taking the slot back off it (see _delayed_restick).
                or was_placed(target.last_message_id or 0)
            ):
                self._buried_since.pop(guild.id, None)
                return None
            content = await self._build(guild)

            # Post the replacement BEFORE removing the old one. Deleting first
            # destroys a working panel whenever the new channel turns out to be
            # unpostable — and if the target *is* the old channel there is
            # nothing left to heal from. Worst case here is two for a moment.
            try:
                message = await target.send(embed=content.embed, view=content.view)
            except discord.HTTPException:
                self._note_failure(guild.id, target.id)
                # Not popping this would make a set max_burial permanently
                # overdue for the guild, degrading the debounce to
                # "first arm wins" (nothing would cancel-and-rearm again).
                self._buried_since.pop(guild.id, None)
                return None
            self._failures.pop(guild.id, None)

            # Record the new id before ANY further await, so the gateway event
            # for our own repost is skipped by should_restick() rather than
            # arming a pointless debounce. Best-effort only — the event is
            # often dispatched before send() even returns — so the self-loop
            # itself is stopped in _delayed_restick(), not here.
            self._remember(guild.id, target.id, message.id)

            old_channel = self._channel(guild, old_channel_id)
            if old_channel is not None and old_message_id:
                await self._delete_old(old_channel, old_message_id)

            if content.signature is not None:
                self._signatures[guild.id] = content.signature
            message_id = message.id
            await asyncio.to_thread(self._save_ids, guild.id, target.id, message_id)

            return message

    def _note_failure(self, guild_id: int, channel_id: int) -> None:
        """Count a failed placement and say so usefully.

        The old log line named neither the guild nor the error, so an operator
        could not tell a 403 (permissions) from a 400 (an over-length embed —
        see ``economy/bounty_views.py``). Both are things an admin can fix, and
        neither was actionable from "could not post panel in <id>".
        """
        count = self._failures.get(guild_id, 0) + 1
        self._failures[guild_id] = count
        if count < self._max_place_failures:
            log.warning(
                "%s: could not post panel in guild %s channel %s (failure %d/%d)",
                self.name, guild_id, channel_id, count, self._max_place_failures,
                exc_info=True,
            )
            return
        if count == self._max_place_failures:
            log.error(
                "%s: giving up on guild %s channel %s after %d consecutive "
                "failed placements — resticks are paused until the panel is "
                "re-posted from the dashboard or the bot restarts",
                self.name, guild_id, channel_id, count,
                exc_info=True,
            )

    async def _delete_old(self, channel: StickyTarget, message_id: int) -> None:
        """Remove the panel we just replaced, retrying a transient failure.

        The two failures used to be one bare ``pass``. They are not the same
        thing: ``NotFound`` is the ordinary case (a member deleted the panel by
        hand, or a previous attempt already removed it), while any other error
        leaves a live panel nothing holds the id for. Persistent views are
        registered by ``custom_id``, so that orphan keeps *working* — a second
        clickable shop panel at last week's prices — and it can only be removed
        by hand once the stored id has moved on.
        """
        try:
            await channel.get_partial_message(message_id).delete()
            return
        except discord.NotFound:
            return
        except discord.HTTPException:
            pass
        # Detached and self-draining on purpose: a queue an owner has to drain
        # is a queue nobody drains (see take_retries, drained by one caller in
        # nine before this pass).
        task = asyncio.create_task(self._retry_delete(channel, message_id))
        self._orphan_tasks.add(task)
        task.add_done_callback(self._orphan_tasks.discard)

    async def _retry_delete(self, channel: StickyTarget, message_id: int) -> None:
        for delay in (2.0, 10.0, 30.0):
            try:
                await asyncio.sleep(delay)
                await channel.get_partial_message(message_id).delete()
                return
            except asyncio.CancelledError:
                raise
            except discord.NotFound:
                return
            except discord.HTTPException:
                continue
        log.warning(
            "%s: could not delete the replaced panel %s in channel %s — it is "
            "orphaned and its buttons stay live",
            self.name, message_id, channel.id,
        )

    async def unpost(self, guild: discord.Guild) -> bool:
        """Delete the panel and forget its placement. True if one was removed."""
        async with self._locks[guild.id]:
            channel_id, message_id = await self.ids(guild.id)
            channel = self._channel(guild, channel_id)
            if channel is not None and message_id:
                await self._delete_old(channel, message_id)
            self.forget(guild.id)
            await asyncio.to_thread(self._save_ids, guild.id, 0, 0)
            return bool(channel_id and message_id)

    async def place_or_refresh(
        self, guild: discord.Guild, target: StickyTarget
    ) -> discord.Message | discord.PartialMessage | None:
        """What a "post the panel" command actually wants.

        Already in ``target`` → edit in place, so re-running the command after a
        re-brand or a re-price refreshes the panel without hopping it to the
        bottom of the channel. Anywhere else, or nowhere yet, or the old message
        is gone → post fresh and drop the old one.

        Unlike ``refresh`` this edits unconditionally (the operator asked for
        it, so "nothing changed" still deserves a reply) and works against the
        channel it was handed rather than re-resolving one from config.

        Returns the live panel, or None if posting failed. An in-place edit
        returns a *partial* message — ``id`` and ``jump_url``, which is all a
        command reply needs.
        """
        channel_id, message_id = await self.ids(guild.id)
        if message_id and channel_id == target.id:
            content = await self._build(guild)
            try:
                await target.get_partial_message(message_id).edit(
                    embed=content.embed, view=content.view
                )
            except discord.NotFound:
                pass  # deleted by hand — fall through to a fresh post
            except discord.HTTPException:
                return None
            else:
                # An operator-driven refresh that worked is proof the channel is
                # usable again, so it clears a paused-after-failures panel.
                self._failures.pop(guild.id, None)
                if content.signature is not None:
                    self._signatures[guild.id] = content.signature
                return target.get_partial_message(message_id)
        return await self.place(guild, target)

    async def refresh(
        self, guild_id: int, *, repost_if_missing: bool = True
    ) -> bool:
        """Edit the panel in place to match current state.

        Skips the API call when ``build`` reports an unchanged signature.
        Returns True when an edit was actually issued.

        ``repost_if_missing`` decides what a deleted panel means. The default
        reposts, so the feature heals itself rather than going quietly dead.
        Pass False where deleting the message is how staff *retire* the panel
        (the economy leaderboard): the stored ids are cleared instead, so the
        caller's loop stops retrying a message that is never coming back.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        channel_id, message_id = await self.ids(guild_id)
        if not channel_id or not message_id:
            return False
        channel = self._channel(guild, channel_id)
        if channel is None:
            return False

        content = await self._build(guild)
        if (
            content.signature is not None
            and self._signatures.get(guild_id) == content.signature
        ):
            return False
        try:
            await channel.get_partial_message(message_id).edit(
                embed=content.embed, view=content.view
            )
        except discord.NotFound:
            if repost_if_missing:
                return await self.place(guild, channel) is not None
            # Re-read before retiring. A sticky repost deletes the old panel and
            # posts a new one, so a 404 here may only mean it *moved* — and this
            # method holds no lock, so a restick can land between the id read
            # above and this edit. Zeroing the ids then would retire a panel that
            # is live in the channel with working buttons, and report it unposted
            # on the dashboard. (The hand-rolled leaderboard refresh this
            # replaced had exactly this guard; dropping it was a regression.)
            if await self.ids(guild_id) != (channel_id, message_id):
                return False
            self.forget(guild_id)
            await asyncio.to_thread(self._save_ids, guild_id, 0, 0)
            log.info(
                "%s: panel for guild %s is gone — cleared its ids",
                self.name, guild_id,
            )
            return False
        except discord.HTTPException:
            # Leave the signature stale and ask any caller-owned loop to retry,
            # so a transient error doesn't strand the panel.
            self._retry.add(guild_id)
            return False
        # A successful edit is proof the channel is usable, so it clears a
        # panel paused by the placement-failure ceiling. Without this a panel
        # whose edits demonstrably work could stay paused on the strength of
        # five old transient send failures.
        self._failures.pop(guild_id, None)
        if content.signature is not None:
            self._signatures[guild_id] = content.signature
        return True

    # ── sticky behaviour ─────────────────────────────────────────────────

    async def on_message(self, message: discord.Message) -> None:
        """Arm a debounced repost when a member posts below the panel.

        Call this from the owning cog's ``on_message`` listener.
        """
        if message.guild is None:
            return
        if message.author.bot:
            if not self._restick_on_bot:
                return
            if was_placed(message.id):
                # Some sticky panel's own repost — skip the doomed debounce.
                # Only an optimisation: this loses the gateway race most of the
                # time. The decision is in _delayed_restick. See was_placed.
                return
        guild_id = message.guild.id
        if self._known is not None and guild_id not in self._known:
            return
        channel_id, panel_message_id = await self._cached_ids(guild_id)
        if not should_restick(
            message_channel_id=message.channel.id,
            message_id=message.id,
            panel_channel_id=channel_id,
            panel_message_id=panel_message_id,
        ):
            return
        self.schedule_restick(guild_id)

    async def on_channel_delete(
        self, channel: discord.abc.GuildChannel | discord.Thread
    ) -> None:
        """Forget a panel whose channel was deleted.

        Nothing breaks without this — ``_channel`` returns None for a dead id
        and the restick returns early, so no loop and no retry — but the stored
        ids outlive the channel, which leaves the dashboard reporting a panel
        that cannot exist. Call it from the cog's ``on_guild_channel_delete``
        and, for a panel whose ``target_types`` include threads, from
        ``on_thread_delete`` as well — Discord dispatches a different event for
        those, so a thread-hosted panel is otherwise never cleaned up.
        """
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        # Under the placement lock: a shielded place() into a *different*
        # channel may be in flight, and if our (0, 0) write landed after its
        # write we would forget a panel that had just been posted — leaving it
        # live in the channel and letting the next post duplicate it.
        async with self._locks[guild.id]:
            channel_id, _message_id = await self.ids(guild.id)
            if not channel_id or channel_id != channel.id:
                return
            pending = self._restick_tasks.pop(guild.id, None)
            if pending is not None and not pending.done():
                pending.cancel()
            self.forget(guild.id)
            await asyncio.to_thread(self._save_ids, guild.id, 0, 0)
        log.info(
            "%s: channel %s was deleted in guild %s — cleared the panel ids",
            self.name, channel.id, guild.id,
        )

    def schedule_restick(self, guild_id: int) -> None:
        """Cancel-and-rearm the debounce so a burst costs one repost."""
        if self._failures.get(guild_id, 0) >= self._max_place_failures:
            return  # paused: see _note_failure
        now = time.monotonic()
        buried_since = self._buried_since.setdefault(guild_id, now)
        existing = self._restick_tasks.get(guild_id)
        if existing is not None and not existing.done():
            if (
                self._max_burial is not None
                and now - buried_since >= self._max_burial
            ):
                # The channel has not fallen quiet for `delay` since the panel
                # was buried, and the ceiling is up: stop re-arming and let the
                # pending repost land. Without a ceiling a conversation with no
                # gaps leaves the panel buried for its whole duration.
                return
            existing.cancel()
        self._restick_tasks[guild_id] = asyncio.create_task(
            self._delayed_restick(guild_id)
        )

    async def _delayed_restick(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return
        try:
            await self._wait_for_hold(guild_id)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            # Only ever maintains an existing panel — never creates one, so a
            # guild that has not configured a channel stays untouched.
            channel_id, message_id = await self._cached_ids(guild_id)
            if not message_id:
                return
            channel = self._channel(guild, channel_id)
            if channel is None:
                return
            # Already at the bottom → nothing to restick. This is what stops a
            # ``restick_on_bot`` panel chasing its own repost forever: the
            # message-id skip in should_restick() only works if _remember() wins
            # a race against the gateway event for that repost, and it usually
            # loses (the MESSAGE_CREATE frame is dispatched while place() is
            # still awaiting the HTTP response). A cheap pre-check on cached
            # ids; ``only_if_buried`` re-decides it under the placement lock,
            # where a queued restick can also see a concurrent placement's
            # result.
            if channel.last_message_id == message_id:
                self._buried_since.pop(guild_id, None)
                return
            # The thing below us is another sticky panel's placement → yield.
            #
            # This, not the check in on_message(), is what actually stops two
            # opted-in panels re-posting each other. _note_placed runs inside
            # _remember, i.e. *after* send() returns, so the on_message check
            # races the gateway exactly as should_restick's id-skip does — and
            # any yield between the frame being dispatched and _remember running
            # loses it, which the awaited HTTP response guarantees. Here we are
            # a whole debounce later, so the registry is reliably populated.
            #
            # Yielding means whoever placed last holds the slot and the other
            # stays above it. Someone has to lose a one-slot contest; a
            # deterministic loser beats the two of them alternating forever. The
            # next genuine trigger re-opens it — exactly one of them takes the
            # slot and the other yields again.
            if was_placed(channel.last_message_id or 0):
                self._buried_since.pop(guild_id, None)
                return
            await self.place(guild, channel, only_if_buried=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: restick failed for guild %s", self.name, guild_id)

    async def _wait_for_hold(self, guild_id: int) -> None:
        """Block while the caller says "not yet".

        Gives up after ``hold_max`` and re-sticks anyway: a hold that never
        clears (a round that never settles, a bug) would otherwise leave the
        panel buried permanently, which is worse than moving it at a bad moment.
        """
        if self._hold is None:
            return
        deadline = time.monotonic() + self._hold_max
        while await self._hold(guild_id):
            if time.monotonic() >= deadline:
                log.info(
                    "%s: hold did not clear within %.0fs for guild %s; "
                    "re-sticking anyway",
                    self.name,
                    self._hold_max,
                    guild_id,
                )
                return
            await asyncio.sleep(self._hold_poll)

    # ── bookkeeping ──────────────────────────────────────────────────────

    def _remember(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self._ref[guild_id] = (
            time.monotonic() + self._cache_ttl,
            channel_id,
            message_id,
        )
        _note_placed(message_id)
        # The panel is the bottom message again, so the burial clock restarts
        # from the next thing that buries it.
        self._buried_since.pop(guild_id, None)
        if self._known is not None:
            self._known.add(guild_id)

    def forget(self, guild_id: int) -> None:
        """Drop cached state for a guild (after an unpost, or a config change)."""
        self._ref.pop(guild_id, None)
        self._signatures.pop(guild_id, None)
        self._retry.discard(guild_id)
        self._failures.pop(guild_id, None)
        self._buried_since.pop(guild_id, None)
        if self._known is not None:
            self._known.discard(guild_id)

    def cancel_all(self) -> None:
        """Cancel pending resticks. Call from ``cog_unload``.

        A placement already in flight finishes anyway (it is shielded), so a
        reload during one still records the panel it posted.
        """
        for task in self._restick_tasks.values():
            task.cancel()
        self._restick_tasks.clear()
        for task in self._orphan_tasks:
            task.cancel()
        self._orphan_tasks.clear()
