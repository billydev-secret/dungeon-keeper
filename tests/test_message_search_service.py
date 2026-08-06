"""Tests for the shared Message Search query service.

The filter surface used to live twice — once in ``/messages/search`` and once in
``/messages/search/export`` — and was only ever exercised through FastAPI. These
tests hit the service directly, which is where the behavior now lives: clause
assembly, the "this can never match" short-circuit, name resolution precedence,
and row hydration.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.services.message_search_service import (
    SORT_ORDERS,
    MessageFilters,
    build_where,
    hydrate_rows,
    reaction_join,
    reaction_select,
    resolve_names,
    resolve_user,
)
from bot_modules.services.message_store import (
    init_known_channels_table,
    init_known_users_table,
    init_message_tables,
    store_message,
    upsert_known_channel,
    upsert_known_user,
)

GUILD = 1
OTHER_GUILD = 2


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_message_tables(c)
    init_known_users_table(c)
    init_known_channels_table(c)
    return c


def _store(conn: sqlite3.Connection, message_id: int, **kw) -> None:
    defaults = dict(
        guild_id=GUILD,
        channel_id=10,
        author_id=50,
        content="hello",
        reply_to_id=None,
        ts=1_000_000,
        attachment_urls=[],
        mention_ids=[],
        sentiment=None,
        emotion=None,
    )
    defaults.update(kw)
    store_message(conn, message_id=message_id, **defaults)  # type: ignore[arg-type]


def _run(conn: sqlite3.Connection, filters: MessageFilters, guild=None) -> list[int]:
    """Apply a filter set and return the matching message ids, newest first."""
    where = build_where(conn, GUILD, filters, guild)
    if where.impossible:
        return []
    rows = conn.execute(
        f"SELECT m.message_id FROM messages m {reaction_join(filters.sort)} "
        f"WHERE {where.sql} ORDER BY {SORT_ORDERS[filters.sort]}",
        where.params,
    ).fetchall()
    return [r[0] for r in rows]


# ── Guild scoping ─────────────────────────────────────────────────────


def test_where_is_always_guild_scoped(conn):
    _store(conn, 1)
    _store(conn, 2, guild_id=OTHER_GUILD)
    assert _run(conn, MessageFilters(include_bots=True)) == [1]


# ── The impossible short-circuit ──────────────────────────────────────


@pytest.mark.parametrize(
    "filters",
    [
        pytest.param(MessageFilters(author=["nobody"]), id="author"),
        pytest.param(MessageFilters(reply_to="nobody"), id="reply_to"),
        pytest.param(MessageFilters(mentions="nobody"), id="mentions"),
    ],
)
def test_unresolvable_name_filter_is_impossible(conn, filters):
    """A name matching nobody must short-circuit, not silently drop the clause.

    Dropping it would return the whole guild — the opposite of what was asked.
    """
    _store(conn, 1)
    where = build_where(conn, GUILD, filters)
    assert where.impossible is True
    assert _run(conn, filters) == []


def test_resolvable_name_filter_is_not_impossible(conn):
    upsert_known_user(conn, GUILD, 50, "benny", "Ben", 1.0)
    _store(conn, 1, author_id=50)
    assert _run(conn, MessageFilters(author=["Ben"], include_bots=True)) == [1]


# ── resolve_user precedence ───────────────────────────────────────────


class _FakeMember:
    def __init__(self, id_: int, name: str, display_name: str):
        self.id = id_
        self.name = name
        self.display_name = display_name


class _FakeGuild:
    def __init__(self, members=(), channels=None):
        self.members = list(members)
        self._channels = channels or {}
        self._by_id = {m.id: m for m in self.members}

    def get_member(self, uid):
        return self._by_id.get(uid)

    def get_channel(self, cid):
        return self._channels.get(cid)


def test_resolve_user_takes_a_numeric_id_at_face_value(conn):
    """A pasted id must reach someone who was never in the name cache."""
    assert resolve_user(conn, "999", GUILD) == [999]


def test_resolve_user_prefers_the_live_guild_over_known_users(conn):
    upsert_known_user(conn, GUILD, 50, "old_name", "Stale", 1.0)
    guild = _FakeGuild([_FakeMember(77, "ben", "Ben")])
    assert resolve_user(conn, "ben", GUILD, guild) == [77]


def test_resolve_user_falls_back_to_known_users_when_guild_misses(conn):
    upsert_known_user(conn, GUILD, 50, "departed", "Departed", 1.0)
    guild = _FakeGuild([_FakeMember(77, "someone", "Someone Else")])
    assert resolve_user(conn, "departed", GUILD, guild) == [50]


def test_resolve_user_works_without_a_guild(conn):
    """Every function must survive a cold or offline bot."""
    upsert_known_user(conn, GUILD, 50, "benny", "Ben", 1.0)
    assert resolve_user(conn, "Ben", GUILD, None) == [50]


def test_resolve_user_is_guild_scoped(conn):
    upsert_known_user(conn, OTHER_GUILD, 50, "benny", "Ben", 1.0)
    assert resolve_user(conn, "Ben", GUILD, None) == []


# ── Individual filters ────────────────────────────────────────────────


def test_multiple_authors_are_deduped(conn):
    _store(conn, 1, author_id=50)
    _store(conn, 2, author_id=51)
    ids = _run(conn, MessageFilters(author=["50", "50", "51"]))
    assert sorted(ids) == [1, 2]


def test_channel_filter(conn):
    _store(conn, 1, channel_id=10)
    _store(conn, 2, channel_id=11)
    assert _run(conn, MessageFilters(channel=["11"], include_bots=True)) == [2]


def test_multiple_channels(conn):
    _store(conn, 1, channel_id=10)
    _store(conn, 2, channel_id=11)
    _store(conn, 3, channel_id=12)
    ids = _run(conn, MessageFilters(channel=["10", "12"], include_bots=True))
    assert sorted(ids) == [1, 3]


def test_timestamp_bounds_are_inclusive(conn):
    _store(conn, 1, ts=100)
    _store(conn, 2, ts=200)
    _store(conn, 3, ts=300)
    ids = _run(conn, MessageFilters(after=200, before=300, include_bots=True))
    assert sorted(ids) == [2, 3]


def test_sentiment_range(conn):
    _store(conn, 1, sentiment=-0.8)
    _store(conn, 2, sentiment=0.0)
    _store(conn, 3, sentiment=0.9)
    ids = _run(
        conn, MessageFilters(sentiment_min=-0.5, sentiment_max=0.5, include_bots=True)
    )
    assert ids == [2]


def test_emotion_filter_accepts_a_comma_list(conn):
    _store(conn, 1, emotion="joy")
    _store(conn, 2, emotion="anger")
    _store(conn, 3, emotion="neutral")
    ids = _run(conn, MessageFilters(emotion="joy,anger", include_bots=True))
    assert sorted(ids) == [1, 2]


def test_emotion_filter_ignores_unknown_values(conn):
    """An unrecognized emotion must not become an unfiltered query."""
    _store(conn, 1, emotion="joy")
    _store(conn, 2, emotion="anger")
    ids = _run(conn, MessageFilters(emotion="bogus", include_bots=True))
    assert sorted(ids) == [1, 2]  # clause dropped, nothing narrowed


@pytest.mark.parametrize(
    "has_attachments,expected", [(True, [1]), (False, [2])], ids=["has", "lacks"]
)
def test_attachment_presence(conn, has_attachments, expected):
    _store(conn, 1, attachment_urls=["https://cdn.example/a.png"])
    _store(conn, 2)
    ids = _run(
        conn,
        MessageFilters(has_attachments=has_attachments, include_bots=True),
    )
    assert ids == expected


@pytest.mark.parametrize(
    "has_reactions,expected", [(True, [1]), (False, [2])], ids=["has", "lacks"]
)
def test_reaction_presence(conn, has_reactions, expected):
    _store(conn, 1)
    _store(conn, 2)
    conn.execute(
        "INSERT INTO message_reactions (message_id, emoji, count) VALUES (1, '👍', 3)"
    )
    ids = _run(conn, MessageFilters(has_reactions=has_reactions, include_bots=True))
    assert ids == expected


def test_length_bounds(conn):
    _store(conn, 1, content="ab")
    _store(conn, 2, content="abcd")
    _store(conn, 3, content="abcdef")
    ids = _run(conn, MessageFilters(min_length=3, max_length=5, include_bots=True))
    assert ids == [2]


def test_mentions_filter(conn):
    _store(conn, 1, mention_ids=[77])
    _store(conn, 2, mention_ids=[88])
    assert _run(conn, MessageFilters(mentions="77", include_bots=True)) == [1]


def test_reply_to_filter_matches_the_parents_author(conn):
    _store(conn, 1, author_id=50)  # the parent
    _store(conn, 2, author_id=51, reply_to_id=1)  # a reply to 50
    _store(conn, 3, author_id=51)  # unrelated
    assert _run(conn, MessageFilters(reply_to="50", include_bots=True)) == [2]


# ── Bot exclusion ─────────────────────────────────────────────────────


def _seed_bot(conn):
    upsert_known_user(conn, GUILD, 900, "botty", "Botty", 1.0, is_bot=True)
    upsert_known_user(conn, GUILD, 50, "benny", "Ben", 1.0)
    _store(conn, 1, author_id=50)
    _store(conn, 2, author_id=900)


def test_bots_are_excluded_by_default(conn):
    _seed_bot(conn)
    assert _run(conn, MessageFilters()) == [1]


def test_include_bots_opts_them_back_in(conn):
    _seed_bot(conn)
    assert sorted(_run(conn, MessageFilters(include_bots=True))) == [1, 2]


def test_an_explicit_bot_author_overrides_the_exclusion(conn):
    """Naming a bot as the author must return it even without include_bots."""
    _seed_bot(conn)
    assert _run(conn, MessageFilters(author=["900"])) == [2]


# ── Sort plumbing ─────────────────────────────────────────────────────


def test_only_most_reacted_pays_for_the_aggregate_join(conn):
    assert reaction_join("most_reacted")
    assert reaction_select("most_reacted")
    for sort in SORT_ORDERS:
        if sort != "most_reacted":
            assert reaction_join(sort) == ""
            assert reaction_select(sort) == ""


def test_most_reacted_orders_by_reaction_total(conn):
    _store(conn, 1)
    _store(conn, 2)
    _store(conn, 3)
    conn.execute(
        "INSERT INTO message_reactions (message_id, emoji, count) VALUES (2, '👍', 5)"
    )
    conn.execute(
        "INSERT INTO message_reactions (message_id, emoji, count) VALUES (3, '🎉', 2)"
    )
    ids = _run(conn, MessageFilters(sort="most_reacted", include_bots=True))
    assert ids == [2, 3, 1]


@pytest.mark.parametrize(
    "sort,expected",
    [
        ("newest", [3, 2, 1]),
        ("oldest", [1, 2, 3]),
        ("longest", [3, 2, 1]),
        ("most_positive", [3, 2, 1]),
        ("most_negative", [1, 2, 3]),
    ],
)
def test_sort_orders(conn, sort, expected):
    _store(conn, 1, ts=100, content="a", sentiment=-0.9)
    _store(conn, 2, ts=200, content="ab", sentiment=0.0)
    _store(conn, 3, ts=300, content="abc", sentiment=0.9)
    assert _run(conn, MessageFilters(sort=sort, include_bots=True)) == expected


# ── Name resolution ───────────────────────────────────────────────────


def test_resolve_names_prefers_live_guild_then_falls_back(conn):
    upsert_known_user(conn, GUILD, 51, "archived", "Archived Name", 1.0)
    upsert_known_channel(conn, GUILD, 11, "archived-channel", 1.0)
    guild = _FakeGuild(
        [_FakeMember(50, "live", "Live Name")],
        channels={10: type("C", (), {"name": "live-channel"})()},
    )
    users, channels = resolve_names(conn, GUILD, {50, 51}, {10, 11}, guild)
    assert users == {50: "Live Name", 51: "Archived Name"}
    assert channels == {10: "live-channel", 11: "archived-channel"}


def test_resolve_names_without_a_guild_uses_only_the_tables(conn):
    upsert_known_user(conn, GUILD, 50, "benny", "Ben", 1.0)
    users, channels = resolve_names(conn, GUILD, {50, 999}, set(), None)
    assert users == {50: "Ben"}
    assert channels == {}


# ── Hydration ─────────────────────────────────────────────────────────


def _rows(conn, order="m.ts DESC"):
    from bot_modules.services.message_search_service import BASE_COLUMNS

    return conn.execute(
        f"SELECT {BASE_COLUMNS} FROM messages m ORDER BY {order}"
    ).fetchall()


def test_hydrate_stringifies_every_snowflake(conn):
    """Snowflakes exceed 2^53 — as JSON numbers they would lose precision."""
    _store(conn, 111111111111111111, channel_id=222222222222222222, author_id=333333333333333333)
    out = hydrate_rows(conn, GUILD, _rows(conn))
    assert out[0]["message_id"] == "111111111111111111"
    assert out[0]["channel_id"] == "222222222222222222"
    assert out[0]["author_id"] == "333333333333333333"


def test_hydrate_names_the_reply_target(conn):
    upsert_known_user(conn, GUILD, 50, "benny", "Ben", 1.0)
    _store(conn, 1, author_id=50)
    _store(conn, 2, author_id=51, reply_to_id=1)
    out = {int(m["message_id"]): m for m in hydrate_rows(conn, GUILD, _rows(conn))}
    assert out[2]["reply_to_id"] == "1"
    assert out[2]["reply_to_author_id"] == "50"
    assert out[2]["reply_to_author_name"] == "Ben"


def test_hydrate_falls_back_to_placeholder_names(conn):
    _store(conn, 1, author_id=50, channel_id=10)
    out = hydrate_rows(conn, GUILD, _rows(conn))
    assert out[0]["author_name"] == "User 50"
    assert out[0]["channel_name"] == "channel 10"


def test_hydrate_attaches_attachment_urls(conn):
    _store(conn, 1, attachment_urls=["https://cdn.example/a.png", "https://cdn.example/b.png"])
    _store(conn, 2, ts=999)
    out = {int(m["message_id"]): m for m in hydrate_rows(conn, GUILD, _rows(conn))}
    assert sorted(out[1]["attachments"]) == [
        "https://cdn.example/a.png",
        "https://cdn.example/b.png",
    ]
    assert out[2]["attachments"] == []


def test_hydrate_renders_null_content_as_empty_string(conn):
    """Storage level ``none`` leaves content NULL; the panel must not print None."""
    _store(conn, 1, content=None)
    out = hydrate_rows(conn, GUILD, _rows(conn))
    assert out[0]["content"] == ""


def test_hydrate_of_no_rows_is_empty(conn):
    assert hydrate_rows(conn, GUILD, []) == []
