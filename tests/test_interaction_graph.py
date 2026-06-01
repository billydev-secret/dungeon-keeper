"""Tests for bot_modules.services.interaction_graph.

interaction_graph.py is 700 statements at ~7% coverage before this file.
The matplotlib renderers are mostly geometry/layout helpers — those are
covered by direct unit tests below. The renderers themselves are smoke-
tested for PNG output without checking pixels.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.interaction_graph import (
    _clean_label,
    _count_crossings,
    _detect_communities,
    _find_components,
    _radial_layout,
    _segments_cross,
    _wrap_label,
    clear_interaction_data,
    init_interaction_tables,
    query_connection_web,
    record_interactions,
    render_connection_web,
    render_interaction_heatmap,
)
from migrations import apply_migrations_sync


# ── Label sanitization ───────────────────────────────────────────────


def test_clean_label_keeps_ascii_unchanged():
    assert _clean_label("Alice") == "Alice"


def test_clean_label_strips_unrenderable_chars():
    # Emoji are unrenderable for DejaVu Sans → stripped.
    cleaned = _clean_label("Hi 🦊 Bob")
    assert "🦊" not in cleaned
    assert "Hi" in cleaned and "Bob" in cleaned


def test_clean_label_returns_placeholder_for_all_unrenderable():
    """If the entire label would strip to empty, return a placeholder."""
    assert _clean_label("🦊🐶🐱") == "[?]"


def test_clean_label_preserves_latin1_supplement():
    """Characters within U+0020–U+024F (Latin Extended A/B) are kept."""
    cleaned = _clean_label("Café Niño")
    assert cleaned == "Café Niño"


def test_wrap_label_breaks_on_spaces():
    """Spaces become newlines so labels wrap inside their node."""
    wrapped = _wrap_label("First Last")
    assert wrapped == "First\nLast"


def test_wrap_label_breaks_on_underscores():
    wrapped = _wrap_label("snake_case_name")
    assert wrapped == "snake\ncase\nname"


# ── DB init and clear ────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """A migrated DB connection ready for interaction-graph tests."""
    path = tmp_path / "ig.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        init_interaction_tables(conn)
        yield conn


def test_init_interaction_tables_is_idempotent(tmp_path):
    """init can be called many times without error."""
    path = tmp_path / "ig.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        init_interaction_tables(conn)
        init_interaction_tables(conn)
        init_interaction_tables(conn)
        # Smoke: tables present
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "user_interactions" in names
        assert "user_interactions_log" in names


def test_clear_interaction_data_removes_only_target_guild(db_conn):
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])
    record_interactions(db_conn, guild_id=20, from_user_id=1, to_user_ids=[2])

    clear_interaction_data(db_conn, guild_id=10)

    rest = db_conn.execute(
        "SELECT COUNT(*) FROM user_interactions"
    ).fetchone()[0]
    assert rest == 1  # only guild 20 left


# ── record_interactions ──────────────────────────────────────────────


def test_record_interactions_inserts_aggregate_and_log(db_conn):
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2, 3])

    rows = db_conn.execute(
        "SELECT from_user_id, to_user_id, weight FROM user_interactions"
        " WHERE guild_id = 10 ORDER BY to_user_id"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, 2, 1), (1, 3, 1)]

    log_rows = db_conn.execute(
        "SELECT COUNT(*) FROM user_interactions_log WHERE guild_id = 10"
    ).fetchone()[0]
    assert log_rows == 2


def test_record_interactions_increments_existing_weight(db_conn):
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], amount=3)

    weight = db_conn.execute(
        "SELECT weight FROM user_interactions"
        " WHERE guild_id = 10 AND from_user_id = 1 AND to_user_id = 2"
    ).fetchone()[0]
    assert weight == 4  # 1 + 3


def test_record_interactions_skips_self_interaction(db_conn):
    """A reply or mention to oneself must not be counted."""
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[1, 2])

    rows = db_conn.execute(
        "SELECT from_user_id, to_user_id FROM user_interactions"
        " WHERE guild_id = 10"
    ).fetchall()
    assert (1, 2) in [(r[0], r[1]) for r in rows]
    assert (1, 1) not in [(r[0], r[1]) for r in rows]


def test_record_interactions_dedupes_via_message_id(db_conn):
    """Same message_id seen twice (live + backfill) must increment only once."""
    record_interactions(
        db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], message_id=500
    )
    record_interactions(
        db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], message_id=500
    )

    weight = db_conn.execute(
        "SELECT weight FROM user_interactions"
        " WHERE guild_id = 10 AND from_user_id = 1 AND to_user_id = 2"
    ).fetchone()[0]
    assert weight == 1  # second insert was a duplicate


def test_record_interactions_without_message_id_does_not_dedupe(db_conn):
    """Without message_id, the unique index doesn't apply — counts increment."""
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])

    weight = db_conn.execute(
        "SELECT weight FROM user_interactions"
        " WHERE guild_id = 10 AND from_user_id = 1 AND to_user_id = 2"
    ).fetchone()[0]
    assert weight == 2


# ── query_connection_web ─────────────────────────────────────────────


def test_query_connection_web_merges_bidirectional_edges(db_conn):
    """A→B (weight 3) and B→A (weight 2) merge into a single undirected edge of 5."""
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], amount=3)
    record_interactions(db_conn, guild_id=10, from_user_id=2, to_user_ids=[1], amount=2)

    edges = query_connection_web(db_conn, guild_id=10, min_weight=1)
    assert len(edges) == 1
    u, v, w = edges[0]
    assert {u, v} == {1, 2}
    assert w == 5


def test_query_connection_web_respects_min_weight(db_conn):
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])
    record_interactions(db_conn, guild_id=10, from_user_id=3, to_user_ids=[4], amount=10)

    edges = query_connection_web(db_conn, guild_id=10, min_weight=5)
    assert len(edges) == 1
    assert edges[0][2] == 10


def test_query_connection_web_limits_to_top_users(db_conn):
    """limit_users restricts the result to the busiest top-N nodes; edges
    where either endpoint falls outside the top-N are excluded."""
    # 100 has many interactions with 200; 300 has fewer. limit_users=2 picks
    # {100, 200}. The 100↔200 edge survives; 100↔300 and 200↔300 are dropped
    # because 300 isn't in the top set.
    record_interactions(
        db_conn, guild_id=10, from_user_id=100, to_user_ids=[200], amount=10
    )
    record_interactions(
        db_conn, guild_id=10, from_user_id=200, to_user_ids=[100], amount=10
    )
    record_interactions(db_conn, guild_id=10, from_user_id=100, to_user_ids=[300])
    record_interactions(db_conn, guild_id=10, from_user_id=200, to_user_ids=[300])

    edges = query_connection_web(db_conn, guild_id=10, limit_users=2)
    node_set = {n for e in edges for n in (e[0], e[1])}
    assert node_set == {100, 200}


def test_query_connection_web_filters_by_after_ts(db_conn):
    """When after_ts is set, only log rows from that time forward are counted."""
    record_interactions(
        db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], ts=100, message_id=1
    )
    record_interactions(
        db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], ts=500, message_id=2
    )

    edges = query_connection_web(db_conn, guild_id=10, after_ts=400)
    assert len(edges) == 1
    # weight from log table = count of matching log rows after the cutoff = 1
    assert edges[0][2] == 1


def test_query_connection_web_returns_empty_on_empty_guild(db_conn):
    edges = query_connection_web(db_conn, guild_id=999)
    assert edges == []


def test_query_connection_web_excludes_self_loops(db_conn):
    """record_interactions skips self loops, but verify the query also drops them."""
    # Force a self-loop directly into the aggregate table (bypassing record_interactions).
    db_conn.execute(
        "INSERT INTO user_interactions (guild_id, from_user_id, to_user_id, weight)"
        " VALUES (10, 1, 1, 5)"
    )
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])

    edges = query_connection_web(db_conn, guild_id=10)
    for u, v, _ in edges:
        assert u != v


# ── _find_components ────────────────────────────────────────────────


def test_find_components_finds_singletons():
    assert _find_components([1, 2, 3], edge_list=[]) == [[1], [2], [3]]


def test_find_components_groups_connected_nodes():
    components = _find_components([1, 2, 3, 4], edge_list=[(1, 2), (2, 3)])
    by_set = sorted(sorted(c) for c in components)
    assert by_set == [[1, 2, 3], [4]]


def test_find_components_handles_disjoint_subgraphs():
    components = _find_components(
        [1, 2, 3, 4, 5, 6], edge_list=[(1, 2), (3, 4), (5, 6)]
    )
    assert len(components) == 3
    for c in components:
        assert len(c) == 2


# ── _detect_communities ─────────────────────────────────────────────


def test_detect_communities_separates_disconnected_clusters():
    """Two cliques with no inter-clique edges → two communities."""
    edges = [
        (1, 2, 10), (2, 3, 10), (1, 3, 10),  # clique A
        (4, 5, 10), (5, 6, 10), (4, 6, 10),  # clique B
    ]
    labels = _detect_communities([1, 2, 3, 4, 5, 6], edges)
    # Clique A members all share a label
    assert labels[1] == labels[2] == labels[3]
    assert labels[4] == labels[5] == labels[6]
    assert labels[1] != labels[4]


def test_detect_communities_labels_singletons_uniquely():
    """A node with no edges gets its own community label."""
    labels = _detect_communities([1, 2, 3], edges=[(1, 2, 1)])
    assert labels[3] != labels[1]


def test_detect_communities_returns_labels_starting_at_zero():
    labels = _detect_communities([1, 2], edges=[(1, 2, 1)])
    assert set(labels.values()) == {0}


# ── _radial_layout ──────────────────────────────────────────────────


def test_radial_layout_places_focus_at_origin():
    pos = _radial_layout(focus_id=1, node_ids=[1, 2, 3], edges=[(1, 2, 1), (1, 3, 1)])
    assert pos[1] == (0.0, 0.0)
    # Direct connections sit at some non-zero radius
    for nid in (2, 3):
        x, y = pos[nid]
        assert (x * x + y * y) > 0


def test_radial_layout_handles_isolated_focus():
    """A focus user with no edges sits alone at origin."""
    pos = _radial_layout(focus_id=1, node_ids=[1], edges=[])
    assert pos == {1: (0.0, 0.0)}


def test_radial_layout_assigns_unreachable_nodes_a_position():
    """Unreachable nodes should still get a coordinate (outermost ring + 1)."""
    pos = _radial_layout(focus_id=1, node_ids=[1, 2, 99], edges=[(1, 2, 1)])
    assert 99 in pos


# ── Geometry: segment crossing ──────────────────────────────────────


def test_segments_cross_returns_true_for_crossing_segments():
    # Segment (0,0)-(10,10) crosses (0,10)-(10,0) at (5,5)
    assert _segments_cross(0, 0, 10, 10, 0, 10, 10, 0) is True


def test_segments_cross_returns_false_for_parallel_segments():
    # Two horizontal segments at different y values — never cross
    assert _segments_cross(0, 0, 10, 0, 0, 5, 10, 5) is False


def test_segments_cross_returns_false_for_disjoint_segments():
    # Two segments far apart
    assert _segments_cross(0, 0, 1, 1, 10, 10, 11, 11) is False


def test_count_crossings_counts_pair_crossings():
    """Two crossing edges → 1; non-crossing → 0."""
    pos = {1: [0.0, 0.0], 2: [10.0, 10.0], 3: [0.0, 10.0], 4: [10.0, 0.0]}
    # Edges (1,2) and (3,4) cross
    assert _count_crossings(pos, [(1, 2), (3, 4)]) == 1


def test_count_crossings_ignores_edges_sharing_endpoints():
    """Edges that share an endpoint can't 'cross' in the layout sense."""
    pos = {1: [0.0, 0.0], 2: [10.0, 10.0], 3: [5.0, 5.0]}
    # (1,2) and (1,3) share node 1 — should not be counted as crossing
    assert _count_crossings(pos, [(1, 2), (1, 3)]) == 0


def test_count_crossings_zero_for_planar_layout():
    """A simple chain layout has no edge crossings."""
    pos = {1: [0.0, 0.0], 2: [1.0, 0.0], 3: [2.0, 0.0], 4: [3.0, 0.0]}
    assert _count_crossings(pos, [(1, 2), (2, 3), (3, 4)]) == 0


# ── Render functions (smoke tests) ──────────────────────────────────


def test_render_connection_web_returns_png_bytes():
    edges = [(1, 2, 5), (2, 3, 3), (1, 3, 2)]
    names = {1: "Alice", 2: "Bob", 3: "Carol"}
    out = render_connection_web(edges, names, guild_name="Test", spread=1.0)
    assert isinstance(out, bytes)
    assert out[:8] == b"\x89PNG\r\n\x1a\n"  # PNG file signature


def test_render_connection_web_raises_on_empty_edges():
    with pytest.raises(ValueError):
        render_connection_web([], {}, guild_name="Test")


def test_render_connection_web_with_focus_user():
    edges = [(1, 2, 5), (1, 3, 4), (2, 3, 2)]
    names = {1: "Alice", 2: "Bob", 3: "Carol"}
    out = render_connection_web(
        edges, names, guild_name="Test", focus_user_id=1
    )
    assert out[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_interaction_heatmap_returns_png_bytes():
    edges = [(1, 2, 5), (2, 3, 3), (1, 3, 2)]
    names = {1: "Alice", 2: "Bob", 3: "Carol"}
    out = render_interaction_heatmap(edges, names, guild_name="Test")
    assert isinstance(out, bytes)
    assert out[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_interaction_heatmap_raises_on_empty():
    with pytest.raises(ValueError):
        render_interaction_heatmap([], {}, guild_name="Test")
