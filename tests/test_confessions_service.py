"""Tests for confession service identity pool helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path


from services.confessions_service import (
    _ANON_ADJECTIVES,
    _ANON_ANIMALS,
    _ANON_CIRCLES,
    _COLOR_POOL_SIZE,
    _NAME_POOL_SIZE,
    anon_circle_from_index,
    anon_name_from_index,
    pop_pool_index,
)
from services.confessions_service import (
    get_ephemeral_anon_identity,
    get_or_assign_anon_identity,
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
