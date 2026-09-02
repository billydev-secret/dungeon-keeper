"""Tests for confession service identity pool helpers."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.anon_audit_service import (
    DEFAULT_RETENTION_DAYS,
    EVENT_CONFESSION_POSTED,
    FEATURE_CONFESSIONS,
    RETENTION_FOREVER,
    SECONDS_PER_DAY,
    insert_event,
    list_events,
    purge_expired,
    set_retention_days,
)
from bot_modules.services.confessions_service import (
    MAX_DISCORD_MESSAGE_LENGTH,
    PENDING_TTL_SECONDS,
    GuildConfig,
    _ANON_ADJECTIVES,
    _ANON_ANIMALS,
    _ANON_CIRCLES,
    _COLOR_POOL_SIZE,
    _NAME_POOL_SIZE,
    _OP_CIRCLE,
    anon_circle_from_index,
    anon_name_from_index,
    build_anon_reply,
    get_ephemeral_anon_identity,
    get_or_assign_anon_identity,
    enqueue_confession,
    get_config,
    get_pending_confession,
    pending_confession_count,
    pending_confessions,
    pop_pool_index,
    purge_expired_pending,
    purge_old_thread_posts,
    resolve_pending_confession,
    upsert_config,
)


def test_anon_name_from_index_first():
    assert anon_name_from_index(0) == f"{_ANON_ADJECTIVES[0]} {_ANON_ANIMALS[0]}"


def test_anon_name_from_index_last():
    last = len(_ANON_ADJECTIVES) * len(_ANON_ANIMALS) - 1
    assert anon_name_from_index(last) == f"{_ANON_ADJECTIVES[-1]} {_ANON_ANIMALS[-1]}"


def test_anon_name_from_index_second_row():
    n_animals = len(_ANON_ANIMALS)
    idx = n_animals + 3  # second adjective, fourth animal
    assert anon_name_from_index(idx) == f"{_ANON_ADJECTIVES[1]} {_ANON_ANIMALS[3]}"


def test_anon_circle_from_index_all():
    for i, circle in enumerate(_ANON_CIRCLES):
        assert anon_circle_from_index(i) == circle


def test_pop_pool_index_no_repeats_color(sync_db_path: Path):
    """Color pool yields all unique indices before repeating."""
    pool_size = _COLOR_POOL_SIZE
    seen: set[int] = set()
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size):
            idx = pop_pool_index(conn, guild_id=1, root_message_id=100, pool_type="color", pool_size=pool_size)
            assert idx not in seen, f"Duplicate index {idx} before pool exhausted"
            seen.add(idx)
    assert seen == set(range(pool_size))


def test_pop_pool_index_refills_after_exhaustion(sync_db_path: Path):
    """After pool is exhausted the next call returns a valid index."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size):
            pop_pool_index(conn, guild_id=1, root_message_id=200, pool_type="color", pool_size=pool_size)
        extra = pop_pool_index(conn, guild_id=1, root_message_id=200, pool_type="color", pool_size=pool_size)
    assert 0 <= extra < pool_size


def test_pop_pool_index_increments_cycle(sync_db_path: Path):
    """cycle column increments when pool refills."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size + 1):
            pop_pool_index(conn, guild_id=1, root_message_id=300, pool_type="color", pool_size=pool_size)
        row = conn.execute(
            "SELECT cycle FROM confession_pools WHERE guild_id=1 AND root_message_id=300 AND pool_type='color'"
        ).fetchone()
    assert row["cycle"] == 1


def test_pop_pool_index_separate_threads_independent(sync_db_path: Path):
    """Pools for different root_message_ids are fully independent."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        idx_a = pop_pool_index(conn, guild_id=1, root_message_id=400, pool_type="color", pool_size=pool_size)
        idx_b = pop_pool_index(conn, guild_id=1, root_message_id=500, pool_type="color", pool_size=pool_size)
    assert 0 <= idx_a < pool_size
    assert 0 <= idx_b < pool_size


def test_pop_pool_index_name_and_color_are_independent(sync_db_path: Path):
    """name and color pools for the same thread are stored as separate DB rows."""
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        n = pop_pool_index(conn, guild_id=1, root_message_id=600, pool_type="name", pool_size=_NAME_POOL_SIZE)
        c = pop_pool_index(conn, guild_id=1, root_message_id=600, pool_type="color", pool_size=_COLOR_POOL_SIZE)
        rows = conn.execute(
            "SELECT pool_type FROM confession_pools WHERE guild_id=1 AND root_message_id=600"
        ).fetchall()
    pool_types = {r["pool_type"] for r in rows}
    assert pool_types == {"name", "color"}
    assert 0 <= n < _NAME_POOL_SIZE
    assert 0 <= c < _COLOR_POOL_SIZE


def test_persistent_identity_stable(sync_db_path: Path):
    """Same user same thread always returns same (name_idx, emoji_idx)."""
    a = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1000, user_id=42)
    b = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1000, user_id=42)
    assert a == b


def test_persistent_identity_unique_per_user(sync_db_path: Path):
    """Two different users in the same thread get different name and color indices."""
    a = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1001, user_id=10)
    b = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1001, user_id=11)
    assert a[0] != b[0], "name_index should differ"
    assert a[1] != b[1], "emoji_index should differ"


def test_persistent_identity_valid_range(sync_db_path: Path):
    name_idx, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1002, user_id=99)
    assert 0 <= name_idx < _NAME_POOL_SIZE
    assert 0 <= emoji_idx < _COLOR_POOL_SIZE


def test_persistent_identity_writes_to_db(sync_db_path: Path):
    get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1003, user_id=77)
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT name_index, emoji_index FROM confession_emoji_assignments "
            "WHERE guild_id=1 AND root_message_id=1003 AND user_id=77"
        ).fetchone()
    assert row is not None
    assert row["name_index"] >= 0
    assert row["emoji_index"] >= 0


def test_persistent_identity_legacy_backfill(sync_db_path: Path):
    """Existing rows with name_index=-1 get a valid name_index on next call."""
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO confession_emoji_assignments (guild_id, root_message_id, user_id, emoji_index, name_index) "
            "VALUES (1, 9999, 55, 3, -1)"
        )
        conn.commit()
    name_idx, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=9999, user_id=55)
    assert name_idx >= 0
    assert emoji_idx == 3  # original emoji preserved


def test_ephemeral_identity_does_not_write_to_assignments(sync_db_path: Path):
    get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2000)
    with sqlite3.connect(str(sync_db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM confession_emoji_assignments WHERE guild_id=1 AND root_message_id=2000"
        ).fetchall()
    assert len(rows) == 0


def test_ephemeral_identity_valid_range(sync_db_path: Path):
    name_idx, emoji_idx = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2001)
    assert 0 <= name_idx < _NAME_POOL_SIZE
    assert 0 <= emoji_idx < _COLOR_POOL_SIZE


def test_ephemeral_identity_advances_pool(sync_db_path: Path):
    """Two consecutive ephemeral calls to the same thread return different indices."""
    a = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2002)
    b = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2002)
    assert a != b


def test_ephemeral_and_persistent_share_pool(sync_db_path: Path):
    """Ephemeral and persistent calls compete for the same color pool (no reuse within a cycle)."""
    pool_size = _COLOR_POOL_SIZE
    seen_colors: set[int] = set()
    root = 3000
    for user_id in range(pool_size // 2):
        _, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=root, user_id=user_id)
        seen_colors.add(emoji_idx)
    for _ in range(pool_size - pool_size // 2):
        _, emoji_idx = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=root)
        seen_colors.add(emoji_idx)
    assert seen_colors == set(range(pool_size))


def test_build_anon_reply_op():
    result = build_anon_reply("hello world", is_op=True)
    assert result.startswith(f"{_OP_CIRCLE} [OP]\n")
    assert "hello world" in result


def test_build_anon_reply_non_op():
    result = build_anon_reply("secret stuff", is_op=False, circle="🔴", anon_name="Brave Owl")
    assert result.startswith("🔴 Brave Owl\n")
    assert "secret stuff" in result


def test_build_anon_reply_truncates():
    long_content = "x" * (MAX_DISCORD_MESSAGE_LENGTH + 100)
    result = build_anon_reply(long_content, is_op=False, circle="🔵", anon_name="Tiny Fox")
    assert len(result) <= MAX_DISCORD_MESSAGE_LENGTH


def test_build_anon_reply_defangs_everyone():
    result = build_anon_reply("@everyone listen up", is_op=False, circle="🟢", anon_name="Quiet Yak")
    assert "@everyone" not in result
    assert "@​everyone" in result


# ── Audit trail vs operational TTL ────────────────────────────────────
#
# The confessions audit panel reads `anon_audit_log`, not `confession_threads`.
# These two lifetimes must stay independent: the operational table is purged at
# a seven-day TTL so thread identity and reply routing stay bounded, and before
# this split that TTL silently truncated the moderation record to a rolling
# week. Since the Discord mod-log channel became optional, the audit row is the
# only durable trace of who is behind an anonymous post.


def test_operational_purge_leaves_audit_trail_intact(sync_db_path: Path):
    guild_id, message_id = 4242, 777
    old = int(time.time()) - (30 * 86400)
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO confession_threads (guild_id, message_id, channel_id,"
            " root_message_id, original_author_id, notify_original_author, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, message_id, 100, message_id, 42, 1, old),
        )
        insert_event(
            conn,
            guild_id=guild_id,
            feature=FEATURE_CONFESSIONS,
            event=EVENT_CONFESSION_POSTED,
            actor_id=42,
            message_id=message_id,
            created_at=old,
        )

    removed = purge_old_thread_posts(sync_db_path)

    assert removed == 1
    with open_db(sync_db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM confession_threads WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0] == 0
        # The moderator-facing record outlives it.
        surviving = list_events(conn, guild_id, feature=FEATURE_CONFESSIONS)
        assert [e.actor_id for e in surviving] == [42]


def test_audit_row_expires_on_its_own_retention_window(sync_db_path: Path):
    """Confessions rows are purged by the anon-audit window like any other."""
    guild_id = 4242
    stale = time.time() - (DEFAULT_RETENTION_DAYS + 1) * SECONDS_PER_DAY
    fresh = time.time()
    with open_db(sync_db_path) as conn:
        for ts in (stale, fresh):
            insert_event(
                conn,
                guild_id=guild_id,
                feature=FEATURE_CONFESSIONS,
                event=EVENT_CONFESSION_POSTED,
                actor_id=42,
                created_at=ts,
            )

    purge_expired(sync_db_path)

    with open_db(sync_db_path) as conn:
        remaining = list_events(conn, guild_id, feature=FEATURE_CONFESSIONS)
    assert len(remaining) == 1
    assert remaining[0].created_at == pytest.approx(fresh)


def test_retention_off_keeps_confessions_forever(sync_db_path: Path):
    """`0` disables purging — the guild's opt-out of a bounded audit window."""
    guild_id = 4242
    ancient = time.time() - (10 * 365 * SECONDS_PER_DAY)
    with open_db(sync_db_path) as conn:
        set_retention_days(conn, guild_id, RETENTION_FOREVER)
        insert_event(
            conn,
            guild_id=guild_id,
            feature=FEATURE_CONFESSIONS,
            event=EVENT_CONFESSION_POSTED,
            actor_id=42,
            created_at=ancient,
        )

    purge_expired(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert len(list_events(conn, guild_id, feature=FEATURE_CONFESSIONS)) == 1


# ── config round-trip ──────────────────────────────────────────────────


def test_config_round_trips_without_an_attachment_dial(sync_db_path: Path):
    """Confessions are submitted through a Discord modal, which has no
    attachment path at all — so there is no attachment cap to store. The
    column survives on live servers; nothing loads or writes it, and the
    upsert still round-trips every setting that is real."""
    from bot_modules.services import confessions_service as cs

    assert not hasattr(cs.GuildConfig, "max_attachments")
    with open_db(sync_db_path) as conn:
        cs._create_tables(conn)
    cs.upsert_config(
        sync_db_path,
        cs.GuildConfig(
            guild_id=77, dest_channel_id=11, log_channel_id=22,
            cooldown_seconds=90, max_chars=500, per_day_limit=3,
            replies_enabled=False, blocked_user_ids=[5],
        ),
    )
    loaded = cs.get_config(sync_db_path, 77)
    assert loaded is not None
    assert (loaded.cooldown_seconds, loaded.max_chars, loaded.per_day_limit) == (90, 500, 3)
    assert loaded.replies_enabled is False
    assert loaded.blocked_set() == {5}
    assert not hasattr(loaded, "max_attachments")


# ── mod-approve mode: the approval queue ──────────────────────────────
#
# `confession_pending` is the only table in the feature that holds a
# confession's text beside its author's real id, so the tests below are as
# much about what it *stops* holding as what it stores.

GUILD = 4242


def _queue(db_path: Path, content="I ate the last biscuit", *, author_id=99, notify=1):
    return enqueue_confession(
        db_path,
        guild_id=GUILD,
        author_id=author_id,
        content=content,
        notify_original_author=notify,
    )


def test_require_approval_round_trips_through_the_config(sync_db_path: Path):
    upsert_config(
        sync_db_path,
        GuildConfig(guild_id=GUILD, dest_channel_id=1, log_channel_id=0,
                    require_approval=True),
    )
    cfg = get_config(sync_db_path, GUILD)
    assert cfg is not None and cfg.require_approval is True


def test_approval_is_off_for_a_guild_that_never_set_it(sync_db_path: Path):
    """The dial ships dark: an existing guild keeps posting straight through."""
    upsert_config(
        sync_db_path,
        GuildConfig(guild_id=GUILD, dest_channel_id=1, log_channel_id=0),
    )
    cfg = get_config(sync_db_path, GUILD)
    assert cfg is not None and cfg.require_approval is False


def test_a_queued_confession_is_listed_for_its_guild(sync_db_path: Path):
    pending_id = _queue(sync_db_path)
    with open_db(sync_db_path) as conn:
        rows = pending_confessions(conn, GUILD)
        assert [r["id"] for r in rows] == [pending_id]
        assert rows[0]["content"] == "I ate the last biscuit"
        assert pending_confession_count(conn, GUILD) == 1


def test_the_queue_never_hands_back_an_author_id(sync_db_path: Path):
    """The privacy seam, at the layer that decides it.

    The board and the picker are moderator surfaces; naming a confession's
    author is admin-only, on the audit panel. Nothing on the approval path is
    given an id it could print, so no renderer downstream can leak one by
    accident.
    """
    pending_id = _queue(sync_db_path, author_id=1234567890)
    with open_db(sync_db_path) as conn:
        listed = pending_confessions(conn, GUILD)
        one = get_pending_confession(conn, GUILD, pending_id)
    assert "author_id" not in listed[0]
    assert one is not None and "author_id" not in one


def test_the_queue_is_scoped_to_one_guild(sync_db_path: Path):
    _queue(sync_db_path)
    enqueue_confession(
        sync_db_path, guild_id=GUILD + 1, author_id=7, content="elsewhere",
        notify_original_author=0,
    )
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 1
        assert [r["content"] for r in pending_confessions(conn, GUILD)] == [
            "I ate the last biscuit"
        ]


def test_the_queue_is_worked_oldest_first(sync_db_path: Path):
    first = _queue(sync_db_path, "first")
    second = _queue(sync_db_path, "second")
    with open_db(sync_db_path) as conn:
        assert [r["id"] for r in pending_confessions(conn, GUILD)] == [first, second]


def test_a_confession_from_another_guild_cannot_be_opened(sync_db_path: Path):
    pending_id = _queue(sync_db_path)
    with open_db(sync_db_path) as conn:
        assert get_pending_confession(conn, GUILD + 1, pending_id) is None


def test_resolving_returns_the_author_and_the_body(sync_db_path: Path):
    """The one call that *does* return an author id — it goes straight to the
    poster or the rejection DM and is never rendered."""
    pending_id = _queue(sync_db_path, author_id=555, notify=1)
    row = resolve_pending_confession(sync_db_path, GUILD, pending_id)
    assert row is not None
    assert row["author_id"] == 555
    assert row["content"] == "I ate the last biscuit"
    assert row["notify_original_author"] == 1


def test_resolving_deletes_the_row(sync_db_path: Path):
    pending_id = _queue(sync_db_path)
    resolve_pending_confession(sync_db_path, GUILD, pending_id)
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 0


def test_a_confession_can_only_be_resolved_once(sync_db_path: Path):
    """Two mods pressing Approve together must not post the same confession
    twice. The claim is the delete, so the loser gets nothing to act on."""
    pending_id = _queue(sync_db_path)
    assert resolve_pending_confession(sync_db_path, GUILD, pending_id) is not None
    assert resolve_pending_confession(sync_db_path, GUILD, pending_id) is None


def test_resolving_is_guild_scoped(sync_db_path: Path):
    pending_id = _queue(sync_db_path)
    assert resolve_pending_confession(sync_db_path, GUILD + 1, pending_id) is None
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 1


def test_an_unreviewed_confession_is_swept_after_a_week(sync_db_path: Path):
    """Not housekeeping: the privacy notice promises the confession-to-author
    link self-destructs in seven days, and a queue nobody works must not
    become the exception."""
    pending_id = _queue(sync_db_path, author_id=555)
    with open_db(sync_db_path) as conn:
        conn.execute(
            "UPDATE confession_pending SET created_at = ? WHERE id = ?",
            (int(time.time()) - PENDING_TTL_SECONDS - 60, pending_id),
        )

    dropped = purge_expired_pending(sync_db_path)

    assert [(r["id"], r["author_id"], r["guild_id"]) for r in dropped] == [
        (pending_id, 555, GUILD)
    ]
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 0


def test_the_sweep_leaves_a_confession_inside_the_window(sync_db_path: Path):
    _queue(sync_db_path)
    assert purge_expired_pending(sync_db_path) == []
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 1


def test_the_sweep_reports_nothing_when_the_queue_is_empty(sync_db_path: Path):
    assert purge_expired_pending(sync_db_path) == []
