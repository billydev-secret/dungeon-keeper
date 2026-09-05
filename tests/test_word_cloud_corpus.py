"""Tests for word cloud corpus reads against the message archive.

The exclusions here are the ones that are not polish: bot authors swamp the
counts, and the archive deliberately outlives Discord deletions, so a query
that ignored ``deleted_at`` would resurface words a member removed on purpose.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.services.message_store import (
    init_known_users_table,
    init_message_tables,
)
from bot_modules.word_cloud.corpus import (
    archive_has_content,
    fetch_archive,
    recent_channel_ids,
)

GUILD = 1000
OTHER_GUILD = 2000
CHANNEL = 10
OTHER_CHANNEL = 11
HUMAN = 100
OTHER_HUMAN = 101
BOT = 200


@pytest.fixture
def conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_message_tables(conn)
    init_known_users_table(conn)
    for guild in (GUILD, OTHER_GUILD):
        for user, is_bot in ((HUMAN, 0), (OTHER_HUMAN, 0), (BOT, 1)):
            conn.execute(
                "INSERT INTO known_users (guild_id, user_id, is_bot) VALUES (?, ?, ?)",
                (guild, user, is_bot),
            )
    return conn


def _add(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    content: str | None = "cats",
    guild_id: int = GUILD,
    channel_id: int = CHANNEL,
    author_id: int = HUMAN,
    ts: int = 5_000,
    sentiment: float | None = None,
    deleted_at: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO messages "
        "(message_id, guild_id, channel_id, author_id, content, ts, sentiment, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, guild_id, channel_id, author_id, content, ts, sentiment, deleted_at),
    )


def _texts(docs) -> list[str]:
    return [d.text for d in docs]


def _fetch(conn, **kw):
    params = dict(
        guild_id=GUILD, channel_ids=[CHANNEL], since_ts=0, cap=100
    )
    params.update(kw)
    return fetch_archive(conn, **params)


def test_reads_stored_content_with_sentiment(conn):
    _add(conn, 1, content="cats and dogs", sentiment=0.5)
    docs = _fetch(conn)
    assert _texts(docs) == ["cats and dogs"]
    assert docs[0].sentiment == pytest.approx(0.5)


def test_excludes_bot_authors(conn):
    _add(conn, 1, content="human words")
    _add(conn, 2, content="embed card copy", author_id=BOT)
    assert _texts(_fetch(conn)) == ["human words"]


def test_excludes_deleted_messages(conn):
    """The archive survives a Discord deletion; a cloud must not resurface it."""
    _add(conn, 1, content="kept")
    _add(conn, 2, content="withdrawn", deleted_at=9_999)
    assert _texts(_fetch(conn)) == ["kept"]


def test_excludes_empty_and_null_content(conn):
    """Content-free guilds leave the column NULL rather than absent."""
    _add(conn, 1, content=None)
    _add(conn, 2, content="")
    _add(conn, 3, content="real")
    assert _texts(_fetch(conn)) == ["real"]


def test_scopes_to_the_requested_channels(conn):
    _add(conn, 1, content="here")
    _add(conn, 2, content="elsewhere", channel_id=OTHER_CHANNEL)
    assert _texts(_fetch(conn)) == ["here"]


def test_reads_several_channels_at_once(conn):
    """Global scope is the readable-channel list, not a missing filter."""
    _add(conn, 1, content="here", ts=1)
    _add(conn, 2, content="elsewhere", channel_id=OTHER_CHANNEL, ts=2)
    docs = _fetch(conn, channel_ids=[CHANNEL, OTHER_CHANNEL])
    assert set(_texts(docs)) == {"here", "elsewhere"}


def test_never_crosses_a_guild_boundary(conn):
    _add(conn, 1, content="ours")
    _add(conn, 2, content="theirs", guild_id=OTHER_GUILD)
    assert _texts(_fetch(conn)) == ["ours"]


def test_respects_the_window(conn):
    _add(conn, 1, content="old", ts=100)
    _add(conn, 2, content="recent", ts=900)
    assert _texts(_fetch(conn, since_ts=500)) == ["recent"]


def test_window_boundary_is_inclusive(conn):
    _add(conn, 1, content="exactly", ts=500)
    assert _texts(_fetch(conn, since_ts=500)) == ["exactly"]


def test_filters_to_one_author_when_named(conn):
    _add(conn, 1, content="mine")
    _add(conn, 2, content="theirs", author_id=OTHER_HUMAN)
    assert _texts(_fetch(conn, author_id=HUMAN)) == ["mine"]


def test_naming_a_bot_author_overrides_the_bot_exclusion(conn):
    """Asking for one account's words means that account, bot or not."""
    _add(conn, 1, content="card copy", author_id=BOT)
    assert _texts(_fetch(conn, author_id=BOT)) == ["card copy"]


def test_cap_keeps_the_newest(conn):
    for i, ts in enumerate((10, 20, 30), start=1):
        _add(conn, i, content=f"m{ts}", ts=ts)
    assert _texts(_fetch(conn, cap=2)) == ["m30", "m20"]


def test_returns_newest_first(conn):
    _add(conn, 1, content="older", ts=10)
    _add(conn, 2, content="newer", ts=20)
    assert _texts(_fetch(conn)) == ["newer", "older"]


def test_no_readable_channels_is_empty_not_an_error(conn):
    """A moderator who can read nothing gets an empty cloud, not a failure."""
    _add(conn, 1)
    assert fetch_archive(
        conn, guild_id=GUILD, channel_ids=[], since_ts=0, cap=100
    ) == []


def test_zero_cap_returns_nothing_rather_than_everything(conn):
    _add(conn, 1)
    assert _fetch(conn, cap=0) == []


def test_archive_has_content_distinguishes_quiet_from_content_free(conn):
    """A quiet week and a guild that stores no text must not read alike."""
    assert archive_has_content(conn, GUILD) is False
    _add(conn, 1, content=None)
    assert archive_has_content(conn, GUILD) is False
    _add(conn, 2, content="words")
    assert archive_has_content(conn, GUILD) is True


def test_archive_has_content_is_per_guild(conn):
    _add(conn, 1, content="words", guild_id=OTHER_GUILD)
    assert archive_has_content(conn, GUILD) is False
    assert archive_has_content(conn, OTHER_GUILD) is True


def test_recent_channel_ids_ranks_by_last_message(conn):
    _add(conn, 1, channel_id=CHANNEL, ts=10)
    _add(conn, 2, channel_id=OTHER_CHANNEL, ts=99)
    assert recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL, OTHER_CHANNEL], limit=2
    ) == [OTHER_CHANNEL, CHANNEL]


def test_recent_channel_ids_respects_the_limit(conn):
    _add(conn, 1, channel_id=CHANNEL, ts=10)
    _add(conn, 2, channel_id=OTHER_CHANNEL, ts=99)
    assert recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL, OTHER_CHANNEL], limit=1
    ) == [OTHER_CHANNEL]


def test_recent_channel_ids_ranks_content_free_guilds_too(conn):
    """The live path needs this precisely where no content is stored."""
    _add(conn, 1, channel_id=CHANNEL, content=None, ts=10)
    _add(conn, 2, channel_id=OTHER_CHANNEL, content=None, ts=99)
    assert recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL, OTHER_CHANNEL], limit=2
    ) == [OTHER_CHANNEL, CHANNEL]


def test_recent_channel_ids_keeps_channels_the_archive_never_saw(conn):
    """A brand-new channel must not become unreachable."""
    _add(conn, 1, channel_id=CHANNEL, ts=10)
    ranked = recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL, OTHER_CHANNEL], limit=5
    )
    assert set(ranked) == {CHANNEL, OTHER_CHANNEL}
    assert ranked[0] == CHANNEL


def test_recent_channel_ids_on_nothing_is_empty(conn):
    assert recent_channel_ids(conn, guild_id=GUILD, channel_ids=[], limit=5) == []
    assert recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL], limit=0
    ) == []
