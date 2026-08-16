"""Migration 163: recovering thread parents from the message archive.

Nothing in the schema ever recorded that a channel id belonged to a thread, so
the historical rows behind the channel analytics could not be attributed after
the fact. One accident of Discord's id scheme makes part of that recoverable
offline: a thread started from a message takes **that message's id as its own**.
So for any thread whose starter message we archived, the parent is a self-join
away — no API call, no guesswork.

The two cases that must not be confused:

  * a thread whose starter lives in another channel — that channel is the parent
  * a forum post, whose id matches its own first message, which lives *inside*
    the post and therefore names no parent at all

Both are threads; only the first can be attributed. Marking the second anyway
still pays off, because ``is_thread`` alone is enough to keep it out of the
channel list when the bot's guild cache is unavailable.
"""

from __future__ import annotations

import sqlite3

import migrations

GUILD = 1469491362444480666
PARENT = 100
THREAD = 555  # also the id of the message it was started from
FORUM_POST = 777  # its own first message's id
PLAIN_CHANNEL = 200


def _apply_before_163(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations,
        "_migration_files",
        lambda: [f for f in real if f.name < "163"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _seed(db_path) -> None:
    conn = sqlite3.connect(db_path)
    for cid in (PARENT, THREAD, FORUM_POST, PLAIN_CHANNEL):
        conn.execute(
            "INSERT INTO known_channels (guild_id, channel_id, channel_name, updated_at)"
            " VALUES (?, ?, '', 0)",
            (GUILD, cid),
        )
    rows = [
        # The message #555 was started from — it sits in the parent channel.
        (THREAD, PARENT),
        # A forum post's own first message, stored against the post itself.
        (FORUM_POST, FORUM_POST),
        # Ordinary traffic that happens to share none of those ids.
        (900, PLAIN_CHANNEL),
    ]
    for message_id, channel_id in rows:
        conn.execute(
            "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts)"
            " VALUES (?, ?, ?, 1, 0)",
            (message_id, GUILD, channel_id),
        )
    conn.commit()
    conn.close()


def _registry(db_path) -> dict[int, tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT channel_id, parent_id, is_thread FROM known_channels WHERE guild_id = ?",
        (GUILD,),
    ).fetchall()
    conn.close()
    return {int(r[0]): (r[1], r[2]) for r in rows}


def test_a_thread_is_matched_to_the_channel_it_was_started_from(tmp_path, monkeypatch):
    db = tmp_path / "m163.db"
    _apply_before_163(db, monkeypatch)
    _seed(db)

    migrations.apply_migrations_sync(db)

    assert _registry(db)[THREAD] == (PARENT, 1)


def test_a_forum_post_is_flagged_a_thread_but_given_no_parent(tmp_path, monkeypatch):
    # Its first message lives inside the post, so the self-join would name the
    # post as its own parent — an infinite rollup if it were allowed through.
    db = tmp_path / "m163.db"
    _apply_before_163(db, monkeypatch)
    _seed(db)

    migrations.apply_migrations_sync(db)

    assert _registry(db)[FORUM_POST] == (None, 1)


def test_an_ordinary_channel_is_left_alone(tmp_path, monkeypatch):
    db = tmp_path / "m163.db"
    _apply_before_163(db, monkeypatch)
    _seed(db)

    migrations.apply_migrations_sync(db)

    registry = _registry(db)
    assert registry[PLAIN_CHANNEL] == (None, 0)
    assert registry[PARENT] == (None, 0)


def test_a_starter_message_in_another_guild_is_not_borrowed(tmp_path, monkeypatch):
    # Snowflakes make a real collision impossible, but the guild predicate is
    # what guarantees that — without it a second guild's message id could
    # invent a parent from a channel this guild cannot even see.
    db = tmp_path / "m163.db"
    _apply_before_163(db, monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO known_channels (guild_id, channel_id, channel_name, updated_at)"
        " VALUES (?, ?, '', 0)",
        (GUILD, THREAD),
    )
    conn.execute(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts)"
        " VALUES (?, 999, ?, 1, 0)",
        (THREAD, PARENT),
    )
    conn.commit()
    conn.close()

    migrations.apply_migrations_sync(db)

    assert _registry(db)[THREAD] == (None, 0)
