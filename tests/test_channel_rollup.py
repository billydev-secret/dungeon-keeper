"""Which channel a message's activity counts toward.

The rules under test are the ones that keep the channel analytics honest: a
thread's messages belong to the channel it was started from, bot-made throwaway
channels belong nowhere, and an id that isn't a channel any more never appears
as a row.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.services.channel_rollup import (
    ChannelResolver,
    build_resolver,
    guild_channel_ids,
    thread_parent_id,
)
from bot_modules.services.message_store import (
    init_known_channels_table,
    upsert_known_channel,
)

GUILD = 1
CHANNEL = 100
THREAD = 101
OTHER_CHANNEL = 200


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_known_channels_table(c)
    yield c
    c.close()


def _resolver(**kwargs) -> ChannelResolver:
    """A resolver with one channel and one thread hanging off it."""
    base = {
        "parents": {THREAD: CHANNEL},
        "threads": frozenset({THREAD}),
        "live_channel_ids": frozenset({CHANNEL, OTHER_CHANNEL}),
        "excluded_ids": frozenset(),
        "live_known": True,
    }
    base.update(kwargs)
    return ChannelResolver(**base)  # type: ignore[arg-type]


# ── Attribution ────────────────────────────────────────────────────────


def test_thread_messages_count_toward_the_parent_channel():
    assert _resolver().resolve(THREAD) == CHANNEL


def test_a_real_channel_counts_as_itself():
    assert _resolver().resolve(CHANNEL) == CHANNEL


def test_a_thread_of_a_deleted_channel_is_dropped():
    # The parent is gone from the guild, so there is nothing to attribute to
    # and the thread must not stand in for it.
    assert _resolver(live_channel_ids=frozenset({OTHER_CHANNEL})).resolve(THREAD) is None


def test_a_thread_with_no_known_parent_is_dropped():
    resolver = _resolver(parents={})
    assert resolver.resolve(THREAD) is None


def test_an_id_the_guild_no_longer_has_is_dropped():
    assert _resolver().resolve(99999) is None


def test_resolve_all_omits_dropped_ids():
    assert _resolver().resolve_all([CHANNEL, THREAD, 99999]) == {
        CHANNEL: CHANNEL,
        THREAD: CHANNEL,
    }


# ── Ephemeral families ─────────────────────────────────────────────────


def test_an_excluded_channel_is_dropped_even_though_it_is_live():
    # Pen-pals rooms and voice rooms are real, current channels — being live is
    # exactly why they need their own rule.
    resolver = _resolver(excluded_ids=frozenset({CHANNEL}))
    assert resolver.resolve(CHANNEL) is None


def test_a_thread_inside_an_excluded_channel_is_dropped():
    resolver = _resolver(excluded_ids=frozenset({CHANNEL}))
    assert resolver.resolve(THREAD) is None


@pytest.mark.parametrize(
    "table, column_sql, row",
    [
        pytest.param(
            "pen_pals_sessions",
            "guild_id INTEGER, channel_id INTEGER",
            (GUILD, CHANNEL),
            id="pen-pals",
        ),
        pytest.param(
            "voice_master_channels",
            "guild_id INTEGER, channel_id INTEGER",
            (GUILD, CHANNEL),
            id="voice-master",
        ),
        pytest.param(
            "jails",
            "guild_id INTEGER, channel_id INTEGER",
            (GUILD, CHANNEL),
            id="jail",
        ),
    ],
)
def test_each_registry_marks_its_channels_ephemeral(conn, table, column_sql, row):
    conn.execute(f"CREATE TABLE {table} ({column_sql})")
    conn.execute(f"INSERT INTO {table} VALUES (?, ?)", row)

    resolver = build_resolver(conn, GUILD, live_channel_ids=[CHANNEL])

    assert resolver.resolve(CHANNEL) is None


def test_a_jailing_that_never_made_a_channel_excludes_nothing(conn):
    # channel_id defaults to 0 for those rows; treating 0 as an id would be
    # harmless today but is exactly the kind of sentinel that leaks later.
    conn.execute("CREATE TABLE jails (guild_id INTEGER, channel_id INTEGER)")
    conn.execute("INSERT INTO jails VALUES (?, 0)", (GUILD,))

    resolver = build_resolver(conn, GUILD, live_channel_ids=[CHANNEL])

    assert resolver.excluded_ids == frozenset()


def test_bios_wizard_channels_are_matched_by_name(conn):
    upsert_known_channel(conn, GUILD, CHANNEL, "bio-556677", 1.0)

    resolver = build_resolver(conn, GUILD, live_channel_ids=[CHANNEL])

    assert resolver.resolve(CHANNEL) is None


@pytest.mark.parametrize("name", ["bios", "bio-templates", "bio-1a", "biography"])
def test_a_channel_that_merely_starts_with_bio_survives(conn, name):
    upsert_known_channel(conn, GUILD, CHANNEL, name, 1.0)

    resolver = build_resolver(conn, GUILD, live_channel_ids=[CHANNEL])

    assert resolver.resolve(CHANNEL) == CHANNEL


def test_a_missing_registry_table_is_not_an_error(conn):
    # A guild that has never used Pen Pals has no such table; the report must
    # still run rather than failing whole.
    resolver = build_resolver(conn, GUILD, live_channel_ids=[CHANNEL])

    assert resolver.resolve(CHANNEL) == CHANNEL


# ── Degraded mode: no guild cache ──────────────────────────────────────


def test_without_a_guild_a_known_thread_still_reaches_its_parent():
    resolver = _resolver(live_channel_ids=frozenset(), live_known=False)
    assert resolver.resolve(THREAD) == CHANNEL


def test_without_a_guild_an_unclassified_id_is_kept():
    # Better a stale row than an empty panel: with no guild we cannot tell a
    # deleted channel from a live one, and blanking the report reads as broken.
    resolver = _resolver(live_channel_ids=frozenset(), live_known=False)
    assert resolver.resolve(99999) == 99999


def test_without_a_guild_a_parentless_thread_is_still_dropped():
    resolver = _resolver(parents={}, live_channel_ids=frozenset(), live_known=False)
    assert resolver.resolve(THREAD) is None


def test_without_a_guild_exclusions_still_apply():
    resolver = _resolver(
        live_channel_ids=frozenset(), live_known=False, excluded_ids=frozenset({CHANNEL})
    )
    assert resolver.resolve(CHANNEL) is None


def test_build_resolver_without_live_ids_enters_degraded_mode(conn):
    assert build_resolver(conn, GUILD, live_channel_ids=None).live_known is False
    assert build_resolver(conn, GUILD, live_channel_ids=[CHANNEL]).live_known is True


def test_an_empty_channel_list_is_treated_as_no_information(conn):
    # Every real guild has at least one channel, so an empty cache means the
    # gateway hasn't filled it yet. Believing it would blank every channel
    # panel until it did.
    resolver = build_resolver(conn, GUILD, live_channel_ids=[])

    assert resolver.live_known is False
    assert resolver.resolve(CHANNEL) == CHANNEL


# ── Reading the registry ───────────────────────────────────────────────


def test_the_registry_round_trips_a_thread_and_its_parent(conn):
    upsert_known_channel(conn, GUILD, CHANNEL, "general", 1.0)
    upsert_known_channel(
        conn, GUILD, THREAD, "a thread", 2.0, parent_id=CHANNEL, is_thread=True
    )

    resolver = build_resolver(conn, GUILD, live_channel_ids=[CHANNEL])

    assert resolver.parents == {THREAD: CHANNEL}
    assert resolver.threads == frozenset({THREAD})
    assert resolver.resolve(THREAD) == CHANNEL


def test_a_known_parent_is_never_unlearned(conn):
    # The backfill can name a parent that the ingest path, seeing only the
    # thread itself, would otherwise overwrite with None on the next message.
    upsert_known_channel(
        conn, GUILD, THREAD, "a thread", 1.0, parent_id=CHANNEL, is_thread=True
    )
    upsert_known_channel(conn, GUILD, THREAD, "renamed", 2.0)

    resolver = build_resolver(conn, GUILD, live_channel_ids=[CHANNEL])

    assert resolver.parents == {THREAD: CHANNEL}


def test_another_guilds_threads_are_not_borrowed(conn):
    upsert_known_channel(
        conn, 2, THREAD, "elsewhere", 1.0, parent_id=CHANNEL, is_thread=True
    )

    resolver = build_resolver(conn, GUILD, live_channel_ids=[CHANNEL])

    assert resolver.parents == {}


# ── discord.py adapters ────────────────────────────────────────────────


def test_thread_parent_id_reads_a_thread():
    class FakeThread:
        parent_id = CHANNEL

    assert thread_parent_id(FakeThread()) == CHANNEL


def test_thread_parent_id_ignores_a_channels_category():
    # TextChannel groups under category_id, never parent_id — the whole reason
    # this test exists is that mistaking one for the other would silently roll
    # every channel up into its category.
    class FakeTextChannel:
        category_id = 999

    assert thread_parent_id(FakeTextChannel()) is None


def test_guild_channel_ids_of_no_guild_is_none():
    assert guild_channel_ids(None) is None


def test_guild_channel_ids_of_a_guild_without_a_channel_list_is_none():
    # A partial guild object has no .channels. Reaching for it threw and took
    # the whole report down; "don't know" degrades instead.
    class PartialGuild:
        id = 1

    assert guild_channel_ids(PartialGuild()) is None


def test_guild_channel_ids_excludes_threads():
    class FakeChannel:
        def __init__(self, id):
            self.id = id

    class FakeGuild:
        channels = [FakeChannel(CHANNEL), FakeChannel(OTHER_CHANNEL)]

    assert guild_channel_ids(FakeGuild()) == [CHANNEL, OTHER_CHANNEL]
