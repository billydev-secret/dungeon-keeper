"""Decide which channel a message's activity counts toward.

The channel analytics — the health roster, the comparison table, the activity
heatmaps, the homepage top-five — all group ``messages`` by ``channel_id`` and
present each group as a channel. Two kinds of id break that reading:

*Threads.* A message posted in a thread carries the thread's own id, so every
thread showed up as its own row in a view meant to answer "which channels are
alive". Threads are throwaway by design, and the conversation they belong to is
the channel they were started from — so their activity is **attributed to the
parent** rather than dropped, keeping it visible where it actually happened.

*Bot-made ephemeral channels.* Pen Pals pairings, Voice Master rooms, jail
channels and bios-wizard rooms are real channels, but each exists to serve one
member or pairing and is deleted afterwards. They have no parent conversation to
roll into, so they are **excluded outright**. Each family is identified from its
own registry rather than by guessing at names — pen_pals_sessions keeps its rows
after a session closes, so even a long-deleted pairing stays recognisable. The
bios wizard is the one exception: it keeps no registry, so its ``bio-<user id>``
channels are matched by name.

Anything the live guild does not list as a current channel is dropped: a deleted
thread, a removed channel, an id we could never classify. That is the deliberate
trade — a handful of unattributable messages disappear (~3% of a 30-day window)
so that no row appears which isn't a channel you could go and read right now.

Resolution is a query-time concern on purpose. The alternative, stamping a
parent onto every ``messages`` row, would answer only for messages ingested
after the change and would need millions of rows rewritten to fix the rest.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

#: Bios-wizard rooms are created as ``bio-<member id>`` and tracked only in
#: memory, so unlike every other ephemeral family they have no registry table to
#: consult. Anchored, and digits-only after the dash, so a hand-made channel
#: called "bio-templates" or "bios" is not swept up with them.
_BIO_WIZARD_NAME = re.compile(r"^bio-\d+$")


@dataclass(frozen=True)
class ChannelResolver:
    """Maps a raw ``messages.channel_id`` to the channel it counts toward.

    ``resolve`` returns the id to attribute activity to, or ``None`` to drop the
    row entirely. Build one with :func:`build_resolver`; the fields are public
    so tests can construct one directly without a database.
    """

    #: thread id -> the channel it was started from
    parents: dict[int, int] = field(default_factory=dict)
    #: ids known to be threads, whether or not we know the parent
    threads: frozenset[int] = frozenset()
    #: ids that exist right now as channels of the live guild
    live_channel_ids: frozenset[int] = frozenset()
    #: pen pals / voice rooms / jail / bios wizard
    excluded_ids: frozenset[int] = frozenset()
    #: False when the bot's guild cache was unavailable, so ``live_channel_ids``
    #: means "we don't know" rather than "these are all of them"
    live_known: bool = True

    def resolve(self, channel_id: int) -> int | None:
        """The channel *channel_id*'s messages belong to, or None to drop them."""
        if channel_id in self.excluded_ids:
            return None

        parent = self.parents.get(channel_id)
        if parent is not None and parent in self.excluded_ids:
            # A thread inside a pen-pals room is as throwaway as the room.
            return None

        if not self.live_known:
            # No guild cache: fall back to what the database alone can say.
            # Deleted channels survive as rows here, which is the lesser evil —
            # dropping everything would render an empty panel and read as a
            # broken report rather than a degraded one.
            if parent is not None:
                return parent
            return None if channel_id in self.threads else channel_id

        if channel_id in self.live_channel_ids:
            return channel_id
        if parent is not None and parent in self.live_channel_ids:
            return parent
        return None

    def resolve_all(self, channel_ids: Iterable[int]) -> dict[int, int]:
        """Resolution map for *channel_ids*, omitting the ones that drop out."""
        out: dict[int, int] = {}
        for cid in channel_ids:
            target = self.resolve(cid)
            if target is not None:
                out[cid] = target
        return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _ephemeral_channel_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    """Ids of bot-made single-purpose channels, from each feature's registry.

    Every lookup is guarded on the table existing: these are separate features
    that a guild may never have used, and a missing table must degrade to "no
    channels of that kind" rather than break the whole report.
    """
    ids: set[int] = set()

    # Pen Pals keeps closed sessions, so this covers every pairing channel ever
    # created — including the ones Discord has long since deleted.
    if _table_exists(conn, "pen_pals_sessions"):
        ids.update(
            int(r[0])
            for r in conn.execute(
                "SELECT channel_id FROM pen_pals_sessions WHERE guild_id = ?",
                (guild_id,),
            )
        )

    # Voice Master deletes a room's row when the room goes, so this only ever
    # holds the live ones. That is enough: a deleted room is not a current guild
    # channel, so it already fails the resolver's live check.
    if _table_exists(conn, "voice_master_channels"):
        ids.update(
            int(r[0])
            for r in conn.execute(
                "SELECT channel_id FROM voice_master_channels WHERE guild_id = ?",
                (guild_id,),
            )
        )

    # Jail rows outlive the release, so released inmates' channels stay known.
    # channel_id defaults to 0 for a jailing that never made one.
    if _table_exists(conn, "jails"):
        ids.update(
            int(r[0])
            for r in conn.execute(
                "SELECT channel_id FROM jails WHERE guild_id = ? AND channel_id > 0",
                (guild_id,),
            )
        )

    # Bios wizard: no registry, so match the name it creates them under.
    if _table_exists(conn, "known_channels"):
        ids.update(
            int(r[0])
            for r in conn.execute(
                "SELECT channel_id, channel_name FROM known_channels WHERE guild_id = ?",
                (guild_id,),
            )
            if _BIO_WIZARD_NAME.match(str(r[1] or ""))
        )

    return ids


def build_resolver(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    live_channel_ids: Collection[int] | None = None,
) -> ChannelResolver:
    """Assemble the resolver for *guild_id*.

    *live_channel_ids* is the id of every channel the guild has right now, from
    the bot's cache. Pass ``None`` when the bot is offline or the guild is not
    cached: the resolver then classifies from the database alone and keeps rows
    it cannot rule out, rather than emptying the report.

    An *empty* list is read the same way as None. Every real guild has at least
    one channel, so an empty cache means the gateway hasn't filled it yet — and
    believing it would blank every channel panel for as long as that lasted.
    """
    parents: dict[int, int] = {}
    threads: set[int] = set()

    # A caller may hand us a database that never created the registry (an
    # in-memory fixture with only the messages table, say). No registry means no
    # thread is known, which the resolver handles as "everything is a channel".
    if _table_exists(conn, "known_channels"):
        for row in conn.execute(
            "SELECT channel_id, parent_id, is_thread FROM known_channels WHERE guild_id = ?",
            (guild_id,),
        ):
            cid = int(row[0])
            if row[2]:
                threads.add(cid)
            if row[1] is not None:
                parents[cid] = int(row[1])

    live_ids = frozenset(int(c) for c in (live_channel_ids or ()))
    return ChannelResolver(
        parents=parents,
        threads=frozenset(threads),
        live_channel_ids=live_ids,
        excluded_ids=frozenset(_ephemeral_channel_ids(conn, guild_id)),
        live_known=bool(live_ids),
    )


def thread_parent_id(channel) -> int | None:
    """The channel a thread hangs off, or None if *channel* is not a thread.

    ``parent_id`` is defined on discord.py's Thread and on no other channel
    type — TextChannel, VoiceChannel and ForumChannel all use ``category_id``
    for their grouping — so this doubles as the thread test without risking a
    channel being rolled into its category.
    """
    return getattr(channel, "parent_id", None)


def guild_channel_ids(guild) -> list[int] | None:
    """Current channel ids of a discord.py guild, or None if there isn't one.

    Threads are deliberately not included — being absent from this list is
    exactly what marks an id as needing attribution or dropping.

    A guild object that carries no channel list at all (a partial guild, a
    stub) is reported as None rather than as an empty guild: not knowing is the
    honest answer, and it degrades the report instead of emptying it.
    """
    if guild is None:
        return None
    channels = getattr(guild, "channels", None)
    if channels is None:
        return None
    return [ch.id for ch in channels]
