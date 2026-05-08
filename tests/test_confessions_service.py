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
