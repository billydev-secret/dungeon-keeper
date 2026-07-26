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
  panel without touching the database.

See ``docs/plans/sticky-panel-extraction.md`` for the full site survey,
including the panels that are deliberately *not* built on this.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, Hashable

import discord

log = logging.getLogger(__name__)

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
    in ``_delayed_restick``.
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
      that is already the channel's last message is never re-sticked, so this
      cannot chase its own repost.

    Then, from the cog: ``on_message`` from a listener, and ``cancel_all()``
    from ``cog_unload``.
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

    # ── state the owning cog can publish ─────────────────────────────────

    def take_retries(self) -> set[int]:
        """Guilds whose last in-place edit failed, cleared as they're returned.

        A caller with a periodic loop drains this so a transient Discord error
        doesn't strand a stale panel until the next mutation. Callers without
        such a loop simply never call it.
        """
        pending, self._retry = self._retry, set()
        return pending

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
    ) -> discord.TextChannel | None:
        """Resolve a stored id to a postable channel in *this* guild.

        Scoped to the guild deliberately: panel ids are dashboard-supplied, and
        a cross-guild lookup would let one server's config point at another's
        channel.
        """
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    # ── placement ────────────────────────────────────────────────────────

    async def place(
        self, guild: discord.Guild, target: discord.TextChannel
    ) -> discord.Message | None:
        """Post a fresh panel at the bottom of ``target``, remove the old one,
        and persist the new ids.

        Returns None when posting fails, leaving any existing panel untouched.
        Serialised per guild, so a manual post and a sticky repost can't race
        into two panels.
        """
        async with self._locks[guild.id]:
            # Re-read the stored ids INSIDE the lock — a caller's pre-lock
            # snapshot can be stale after a racing post, and deleting it would
            # orphan the live panel.
            old_channel_id, old_message_id = await self.ids(guild.id)
            content = await self._build(guild)

            # Post the replacement BEFORE removing the old one. Deleting first
            # destroys a working panel whenever the new channel turns out to be
            # unpostable — and if the target *is* the old channel there is
            # nothing left to heal from. Worst case here is two for a moment.
            try:
                message = await target.send(embed=content.embed, view=content.view)
            except discord.HTTPException:
                log.warning("%s: could not post panel in %s", self.name, target.id)
                return None

            # Record the new id before ANY further await, so the gateway event
            # for our own repost is skipped by should_restick() rather than
            # arming a pointless debounce. Best-effort only — the event is
            # often dispatched before send() even returns — so the self-loop
            # itself is stopped in _delayed_restick(), not here.
            self._remember(guild.id, target.id, message.id)

            old_channel = self._channel(guild, old_channel_id)
            if old_channel is not None and old_message_id:
                try:
                    await old_channel.get_partial_message(old_message_id).delete()
                except discord.HTTPException:
                    pass

            if content.signature is not None:
                self._signatures[guild.id] = content.signature
            message_id = message.id
            await asyncio.to_thread(self._save_ids, guild.id, target.id, message_id)

            return message

    async def unpost(self, guild: discord.Guild) -> bool:
        """Delete the panel and forget its placement. True if one was removed."""
        async with self._locks[guild.id]:
            channel_id, message_id = await self.ids(guild.id)
            channel = self._channel(guild, channel_id)
            if channel is not None and message_id:
                try:
                    await channel.get_partial_message(message_id).delete()
                except discord.HTTPException:
                    pass
            self.forget(guild.id)
            await asyncio.to_thread(self._save_ids, guild.id, 0, 0)
            return bool(channel_id and message_id)

    async def place_or_refresh(
        self, guild: discord.Guild, target: discord.TextChannel
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
                if content.signature is not None:
                    self._signatures[guild.id] = content.signature
                return target.get_partial_message(message_id)
        return await self.place(guild, target)

    async def refresh(self, guild_id: int) -> bool:
        """Edit the panel in place to match current state.

        Skips the API call when ``build`` reports an unchanged signature.
        Returns True when an edit was actually issued.
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
            # Deleted out from under us — repost so the feature heals itself
            # rather than going quietly dead.
            return await self.place(guild, channel) is not None
        except discord.HTTPException:
            # Leave the signature stale and ask any caller-owned loop to retry,
            # so a transient error doesn't strand the panel.
            self._retry.add(guild_id)
            return False
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
        if message.author.bot and not self._restick_on_bot:
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

    def schedule_restick(self, guild_id: int) -> None:
        """Cancel-and-rearm the debounce so a burst costs one repost."""
        existing = self._restick_tasks.get(guild_id)
        if existing is not None and not existing.done():
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
            # still awaiting the HTTP response). Here both paths have converged,
            # so the check is decided rather than raced. Free, too —
            # last_message_id is gateway-maintained, not an API call.
            if channel.last_message_id == message_id:
                return
            await self.place(guild, channel)
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
        if self._known is not None:
            self._known.add(guild_id)

    def forget(self, guild_id: int) -> None:
        """Drop cached state for a guild (after an unpost, or a config change)."""
        self._ref.pop(guild_id, None)
        self._signatures.pop(guild_id, None)
        self._retry.discard(guild_id)
        if self._known is not None:
            self._known.discard(guild_id)

    def cancel_all(self) -> None:
        """Cancel pending resticks. Call from ``cog_unload``."""
        for task in self._restick_tasks.values():
            task.cancel()
        self._restick_tasks.clear()
