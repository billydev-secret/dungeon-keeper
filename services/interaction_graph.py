"""Interaction graph — replies and mentions between users.

Stores pairwise interaction weights and renders network charts:
  - Community-clustered spring layout (server-wide view)
  - Radial ego layout (focused-member view)
  - Adjacency heatmap (/interaction_heatmap)
"""

from __future__ import annotations

import io
import math
import random as _random
import re
import sqlite3
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")

# Persistent pool — avoids spawning 4 threads on every /connection_web call.
_layout_executor = ThreadPoolExecutor(max_workers=4)

# Strip characters that DejaVu Sans (matplotlib default) cannot render.
# DejaVu Sans covers the Basic Latin + Latin-1 Supplement blocks reliably.
# Anything outside U+0020–U+024F is a candidate for box rendering or a
# freetype crash, so we allow only that range plus common punctuation.
_UNRENDERABLE_RE = re.compile(
    "["
    "\U00010000-\U0010ffff"  # supplementary planes (emoji, etc.)
    "\u0250-\u2dff"  # extended Latin and everything up to CJK
    "\u2e00-\ufdff"  # misc punctuation through Arabic
    "\ufe00-\uffff"  # variation selectors, specials
    "]+",
    flags=re.UNICODE,
)


def _clean_label(name: str) -> str:
    cleaned = _UNRENDERABLE_RE.sub("", name).strip()
    return cleaned if cleaned else "[?]"  # never return an unrenderable string


def _wrap_label(name: str) -> str:
    """Break a cleaned label on spaces or underscores so it wraps inside a node."""
    cleaned = _clean_label(name)
    return re.sub(r"[ _]+", "\n", cleaned)


# Discord dark theme palette (shared with activity_graphs.py)
_BG = "#2f3136"
_TEXT = "#dcddde"
_GRID = "#40444b"
_NODE = "#5865f2"
_NODE_EDGE = "#99aab5"
_EDGE_COLOR = "#99aab5"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def init_interaction_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_interactions (
            guild_id     INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            to_user_id   INTEGER NOT NULL,
            weight       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, from_user_id, to_user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_interactions_log (
            guild_id     INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            to_user_id   INTEGER NOT NULL,
            ts           INTEGER NOT NULL,
            message_id   INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_interactions_log_guild_ts
        ON user_interactions_log (guild_id, ts)
        """
    )
    # Partial unique index: deduplicates rows that have a message_id so that
    # running /interaction_scan multiple times (or while the bot is live) does
    # not inflate the counts.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_interactions_log_dedup
        ON user_interactions_log (guild_id, message_id, from_user_id, to_user_id)
        WHERE message_id IS NOT NULL
        """
    )
    # Migration for existing databases that pre-date the message_id column.
    try:
        conn.execute("ALTER TABLE user_interactions_log ADD COLUMN message_id INTEGER")
    except Exception:
        pass  # Column already exists


def clear_interaction_data(conn: sqlite3.Connection, guild_id: int) -> None:
    """Delete all interaction records for a guild (both aggregate and log tables)."""
    conn.execute("DELETE FROM user_interactions WHERE guild_id = ?", (guild_id,))
    conn.execute("DELETE FROM user_interactions_log WHERE guild_id = ?", (guild_id,))


def record_interactions(
    conn: sqlite3.Connection,
    guild_id: int,
    from_user_id: int,
    to_user_ids: list[int],
    amount: int = 1,
    ts: int | None = None,
    message_id: int | None = None,
) -> None:
    """Increment the interaction weight from *from_user_id* to each target.

    ts         – Unix timestamp of the interaction; defaults to now.
    message_id – Discord message ID.  When provided, the unique index on the
                 log table prevents the same message from being counted twice
                 (guards against scan + live-recording overlap, and repeated
                 scan runs).  The aggregate table is only updated when the log
                 insert is genuinely new.
    """
    ts = ts if ts is not None else int(_time.time())
    for to_user_id in to_user_ids:
        if to_user_id == from_user_id:
            continue
        result = conn.execute(
            """
            INSERT OR IGNORE INTO user_interactions_log
                (guild_id, from_user_id, to_user_id, ts, message_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, from_user_id, to_user_id, ts, message_id),
        )
        if result.rowcount == 0:
            # Duplicate message — already counted; skip aggregate update too.
            continue
        conn.execute(
            """
            INSERT INTO user_interactions (guild_id, from_user_id, to_user_id, weight)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, from_user_id, to_user_id)
            DO UPDATE SET weight = weight + excluded.weight
            """,
            (guild_id, from_user_id, to_user_id, amount),
        )


def query_connection_web(
    conn: sqlite3.Connection,
    guild_id: int,
    min_weight: int = 1,
    limit_users: int = 40,
    after_ts: int | None = None,
) -> list[tuple[int, int, int]]:
    """
    Return directed edges as (from_user_id, to_user_id, combined_weight).

    Combined weight merges A→B and B→A into one undirected edge so the
    chart shows total interaction between each pair.

    Restricted to the top *limit_users* by total interaction volume to keep
    the chart readable.

    after_ts – if set, only count interactions recorded at or after this Unix
               timestamp (queries the log table).  None means all-time
               (queries the faster aggregate table).
    """
    if after_ts is not None:
        rows = conn.execute(
            """
            WITH top_users AS (
                SELECT user_id FROM (
                    SELECT from_user_id AS user_id, COUNT(*) AS w
                    FROM user_interactions_log WHERE guild_id = ? AND ts >= ?
                    GROUP BY from_user_id
                    UNION ALL
                    SELECT to_user_id AS user_id, COUNT(*) AS w
                    FROM user_interactions_log WHERE guild_id = ? AND ts >= ?
                    GROUP BY to_user_id
                )
                GROUP BY user_id ORDER BY SUM(w) DESC LIMIT ?
            )
            SELECT from_user_id, to_user_id, COUNT(*) AS weight
            FROM user_interactions_log
            WHERE guild_id = ? AND ts >= ?
              AND from_user_id IN (SELECT user_id FROM top_users)
              AND to_user_id   IN (SELECT user_id FROM top_users)
            GROUP BY from_user_id, to_user_id
            ORDER BY weight DESC
            """,
            (guild_id, after_ts, guild_id, after_ts, limit_users, guild_id, after_ts),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            WITH top_users AS (
                SELECT user_id FROM (
                    SELECT from_user_id AS user_id, SUM(weight) AS w
                    FROM user_interactions WHERE guild_id = ?
                    GROUP BY from_user_id
                    UNION ALL
                    SELECT to_user_id AS user_id, SUM(weight) AS w
                    FROM user_interactions WHERE guild_id = ?
                    GROUP BY to_user_id
                )
                GROUP BY user_id ORDER BY SUM(w) DESC LIMIT ?
            )
            SELECT from_user_id, to_user_id, weight
            FROM user_interactions
            WHERE guild_id = ?
              AND from_user_id IN (SELECT user_id FROM top_users)
              AND to_user_id   IN (SELECT user_id FROM top_users)
            ORDER BY weight DESC
            """,
            (guild_id, guild_id, limit_users, guild_id),
        ).fetchall()

    # Merge A→B and B→A into a single undirected edge
    merged: dict[tuple[int, int], int] = {}
    for r in rows:
        u, v, w = int(r[0]), int(r[1]), int(r[2])
        key = (min(u, v), max(u, v))
        merged[key] = merged.get(key, 0) + w

    return [(u, v, w) for (u, v), w in merged.items() if w >= min_weight and u != v]


# ---------------------------------------------------------------------------
# Connected-component helpers
# ---------------------------------------------------------------------------


def _find_components(
    node_ids: list[int],
    edge_list: list[tuple[int, int]],
) -> list[list[int]]:
    """Return connected components as lists of node IDs (iterative DFS)."""
    adj: dict[int, list[int]] = {nid: [] for nid in node_ids}
    for u, v in edge_list:
        adj[u].append(v)
        adj[v].append(u)
    visited: set[int] = set()
    components: list[list[int]] = []
    for nid in node_ids:
        if nid in visited:
            continue
        comp: list[int] = []
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp.append(cur)
            stack.extend(adj[cur])
        components.append(comp)
    return components


# ---------------------------------------------------------------------------
# Community detection (Louvain-style label propagation)
# ---------------------------------------------------------------------------


def _detect_communities(
    node_ids: list[int],
    edges: list[tuple[int, int, int]],
) -> dict[int, int]:
    """Assign each node a community label via weighted label propagation.

    Returns {node_id: community_label}.  Community labels are arbitrary ints
    (one of the node IDs in the community).  Runs until convergence or
    max_iterations.
    """
    # Build weighted adjacency
    adj: dict[int, list[tuple[int, int]]] = {nid: [] for nid in node_ids}
    for u, v, w in edges:
        adj[u].append((v, w))
        adj[v].append((u, w))

    # Initialize each node in its own community
    label: dict[int, int] = {nid: nid for nid in node_ids}

    order = list(node_ids)
    for _ in range(50):
        _random.shuffle(order)
        changed = False
        for nid in order:
            if not adj[nid]:
                continue
            # Weighted vote from neighbours
            votes: dict[int, int] = {}
            for nb, w in adj[nid]:
                votes[label[nb]] = votes.get(label[nb], 0) + w
            best_label = max(votes, key=lambda k: votes[k])
            if best_label != label[nid]:
                label[nid] = best_label
                changed = True
        if not changed:
            break

    # Renumber communities to 0, 1, 2, ...
    unique = sorted(set(label.values()))
    remap = {old: i for i, old in enumerate(unique)}
    return {nid: remap[label[nid]] for nid in node_ids}


# Community colour palette — distinct, muted colours that sit well on dark bg.
_COMMUNITY_COLORS = [
    "#5865f2",  # blurple
    "#57f287",  # green
    "#fee75c",  # yellow
    "#eb459e",  # pink
    "#ed4245",  # red
    "#3ba5f7",  # light blue
    "#e67e22",  # orange
    "#9b59b6",  # purple
    "#1abc9c",  # teal
    "#e91e63",  # magenta
    "#2ecc71",  # emerald
    "#f39c12",  # amber
]


# ---------------------------------------------------------------------------
# Radial / ego layout
# ---------------------------------------------------------------------------


def _radial_layout(
    focus_id: int,
    node_ids: list[int],
    edges: list[tuple[int, int, int]],
) -> dict[int, tuple[float, float]]:
    """Lay out nodes in concentric rings around *focus_id*.

    Ring 0 = focus user (centre).
    Ring 1 = direct connections, spread evenly.
    Ring 2+ = connections of connections, etc.

    Within each ring nodes are evenly spaced; heavily-connected nodes get a
    slight angular bias toward their strongest connection in the inner ring
    so related nodes cluster together.
    """
    # BFS to find ring assignments
    adj: dict[int, list[tuple[int, int]]] = {nid: [] for nid in node_ids}
    for u, v, w in edges:
        adj[u].append((v, w))
        adj[v].append((u, w))

    ring: dict[int, int] = {focus_id: 0}
    frontier = [focus_id]
    max_ring = 0
    while frontier:
        nxt: list[int] = []
        for nid in frontier:
            for nb, _ in adj[nid]:
                if nb not in ring:
                    ring[nb] = ring[nid] + 1
                    max_ring = max(max_ring, ring[nb])
                    nxt.append(nb)
        frontier = nxt

    # Unreachable nodes get outermost ring + 1
    for nid in node_ids:
        if nid not in ring:
            ring[nid] = max_ring + 1

    # Group nodes by ring
    rings: dict[int, list[int]] = {}
    for nid, r in ring.items():
        rings.setdefault(r, []).append(nid)

    pos: dict[int, tuple[float, float]] = {focus_id: (0.0, 0.0)}

    if max_ring == 0:
        return pos

    # For rings beyond 0, sort nodes by their strongest inner-ring neighbour's
    # angle so related nodes sit near each other.
    inner_angle: dict[int, float] = {focus_id: 0.0}

    for r in range(1, max(rings.keys()) + 1):
        nodes = rings.get(r, [])
        if not nodes:
            continue

        # Compute an angular anchor for each node based on inner connections
        def _anchor(nid: int) -> float:
            best_w = 0
            best_angle = 0.0
            for nb, w in adj[nid]:
                if ring.get(nb, r) < r and nb in inner_angle:
                    if w > best_w:
                        best_w = w
                        best_angle = inner_angle[nb]
            return best_angle

        nodes.sort(key=_anchor)

        radius = r * (1.0 / max(max_ring, 1))
        n = len(nodes)
        for i, nid in enumerate(nodes):
            angle = 2 * math.pi * i / n
            # Slight bias toward anchor angle
            anchor = _anchor(nid)
            blended = 0.7 * angle + 0.3 * anchor if n > 2 else angle
            x = radius * math.cos(blended)
            y = radius * math.sin(blended)
            pos[nid] = (x, y)
            inner_angle[nid] = blended

    return pos


# ---------------------------------------------------------------------------
# Spring layout
# ---------------------------------------------------------------------------


def _run_fr(
    node_ids: list[int],
    pos: dict[int, list[float]],
    weight_map: dict[tuple[int, int], float],
    max_w: float,
    k: float,
    iterations: int,
) -> None:
    """Run Fruchterman-Reingold in-place on *pos* for *iterations* steps."""
    n = len(node_ids)
    for step in range(iterations):
        # Temperature calibrated to k so early moves are ≤2× the ideal
        # edge length rather than the previous fixed 1.0 (which was ~3× k
        # for typical graph sizes and caused oscillation).
        t = max(k * 0.01, k * 2.0 * (1 - step / iterations))
        disp: dict[int, list[float]] = {nid: [0.0, 0.0] for nid in node_ids}

        for i in range(n):
            u = node_ids[i]
            for j in range(i + 1, n):
                v = node_ids[j]
                dx = pos[u][0] - pos[v][0]
                dy = pos[u][1] - pos[v][1]
                d = math.sqrt(dx * dx + dy * dy) or 1e-6
                rep = k * k / d
                disp[u][0] += rep * dx / d
                disp[u][1] += rep * dy / d
                disp[v][0] -= rep * dx / d
                disp[v][1] -= rep * dy / d

        for (eu, ev), ew in weight_map.items():
            # Scale purely proportional to weight — old formula had a 0.15
            # floor that pulled even near-zero-weight edges strongly.
            scale = 0.5 * (ew / max_w)
            dx = pos[eu][0] - pos[ev][0]
            dy = pos[eu][1] - pos[ev][1]
            d = math.sqrt(dx * dx + dy * dy) or 1e-6
            attr = (d * d / k) * scale
            disp[eu][0] -= attr * dx / d
            disp[eu][1] -= attr * dy / d
            disp[ev][0] += attr * dx / d
            disp[ev][1] += attr * dy / d

        # Weak gravity toward origin — prevents the layout drifting
        # asymmetrically when repulsion forces don't sum to zero.
        g = k * 0.01
        for nid in node_ids:
            disp[nid][0] -= pos[nid][0] * g
            disp[nid][1] -= pos[nid][1] * g

        for nid in node_ids:
            mag = math.sqrt(disp[nid][0] ** 2 + disp[nid][1] ** 2) or 1e-6
            capped = min(mag, t)
            pos[nid][0] += disp[nid][0] / mag * capped
            pos[nid][1] += disp[nid][1] / mag * capped


def _layout_energy(
    pos: dict[int, list[float]],
    edge_pairs: set[tuple[int, int]],
    node_ids: list[int],
    k: float,
) -> float:
    """Fruchterman-Reingold energy — lower means less tangled."""
    energy = 0.0
    n = len(node_ids)
    for i in range(n):
        u = node_ids[i]
        for j in range(i + 1, n):
            v = node_ids[j]
            dx = pos[u][0] - pos[v][0]
            dy = pos[u][1] - pos[v][1]
            d = math.sqrt(dx * dx + dy * dy) or 1e-6
            key = (min(u, v), max(u, v))
            if key in edge_pairs:
                energy += (d - k) ** 2  # edge: penalise deviation from k
            else:
                energy += k * k / d  # non-edge: penalise closeness
    return energy


# ---------------------------------------------------------------------------
# Crossing minimization helpers
# ---------------------------------------------------------------------------


def _segments_cross(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
    dx: float,
    dy: float,
) -> bool:
    """Return True if segment AB properly crosses segment CD."""

    def _side(
        ox: float, oy: float, px: float, py: float, qx: float, qy: float
    ) -> float:
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    d1 = _side(cx, cy, dx, dy, ax, ay)
    d2 = _side(cx, cy, dx, dy, bx, by)
    d3 = _side(ax, ay, bx, by, cx, cy)
    d4 = _side(ax, ay, bx, by, dx, dy)
    return ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4))


def _count_crossings(
    pos: dict[int, list[float]],
    edge_list: list[tuple[int, int]],
) -> int:
    """Count proper edge crossings in *pos* — O(E²)."""
    m = len(edge_list)
    count = 0
    for i in range(m):
        u1, v1 = edge_list[i]
        ax, ay = pos[u1][0], pos[u1][1]
        bx, by = pos[v1][0], pos[v1][1]
        for j in range(i + 1, m):
            u2, v2 = edge_list[j]
            if u1 in (u2, v2) or v1 in (u2, v2):
                continue
            if _segments_cross(
                ax, ay, bx, by, pos[u2][0], pos[u2][1], pos[v2][0], pos[v2][1]
            ):
                count += 1
    return count


def _incident_crossings(
    pos: dict[int, list[float]],
    edge_list: list[tuple[int, int]],
    incident: list[int],
) -> int:
    """Count crossings involving at least one edge from *incident* (edge indices)."""
    incident_set = set(incident)
    m = len(edge_list)
    count = 0
    for idx_i in incident:
        u1, v1 = edge_list[idx_i]
        ax, ay = pos[u1][0], pos[u1][1]
        bx, by = pos[v1][0], pos[v1][1]
        for j in range(m):
            if j == idx_i:
                continue
            if j in incident_set and j < idx_i:
                continue  # pair already counted from the other direction
            u2, v2 = edge_list[j]
            if u1 in (u2, v2) or v1 in (u2, v2):
                continue
            if _segments_cross(
                ax, ay, bx, by, pos[u2][0], pos[u2][1], pos[v2][0], pos[v2][1]
            ):
                count += 1
    return count


def _swap_to_reduce_crossings(
    node_ids: list[int],
    pos: dict[int, list[float]],
    edge_list: list[tuple[int, int]],
    max_passes: int = 5,
) -> None:
    """
    Greedy post-layout pass: swap pairs of node positions to reduce crossings.

    For each candidate swap (u, v) only edges incident to u or v can change
    crossing status, so the check is much cheaper than a full recount.
    Repeats until no swap helps or *max_passes* is exhausted.
    """
    n = len(node_ids)
    if n < 4 or not edge_list:
        return

    adj: dict[int, list[int]] = {nid: [] for nid in node_ids}
    for k, (u, v) in enumerate(edge_list):
        adj[u].append(k)
        adj[v].append(k)
    # Pre-build frozen sets so the inner loop doesn't allocate per pair.
    adj_set: dict[int, frozenset[int]] = {nid: frozenset(adj[nid]) for nid in node_ids}

    for _ in range(max_passes):
        improved = False
        for i in range(n):
            for j in range(i + 1, n):
                u, v = node_ids[i], node_ids[j]
                uv_edge = (min(u, v), max(u, v))
                incident = [
                    k
                    for k in (adj_set[u] | adj_set[v])
                    if edge_list[k] != uv_edge  # skip the invariant u-v edge
                ]
                if not incident:
                    continue

                before = _incident_crossings(pos, edge_list, incident)
                pos[u], pos[v] = pos[v], pos[u]
                after = _incident_crossings(pos, edge_list, incident)

                if after < before:
                    improved = True
                else:
                    pos[u], pos[v] = pos[v], pos[u]  # revert

        if not improved:
            break


def _nudge_to_reduce_crossings(
    node_ids: list[int],
    pos: dict[int, list[float]],
    edge_list: list[tuple[int, int]],
    max_passes: int = 3,
) -> None:
    """
    Fine-grained position search run after _swap_to_reduce_crossings.

    For each node, tries 12 directions × 4 radii (48 candidates) around its
    current position and keeps whichever reduces incident crossings.  All
    candidates are offsets from the node's own position, so no two nodes can
    ever land on the same coordinates (which would cause an edge between them
    to render as a self-loop dot).  Large-scale relocation is left to the
    swap pass.
    """
    n = len(node_ids)
    if n < 4 or not edge_list:
        return

    adj: dict[int, list[int]] = {nid: [] for nid in node_ids}
    for k, (u, v) in enumerate(edge_list):
        adj[u].append(k)
        adj[v].append(k)

    xs = [pos[nid][0] for nid in node_ids]
    ys = [pos[nid][1] for nid in node_ids]
    ring_r = max(max(xs) - min(xs), max(ys) - min(ys)) / max(n - 1, 1)

    for _ in range(max_passes):
        improved = False
        for nid in node_ids:
            incident = adj[nid]
            if not incident:
                continue

            orig_x, orig_y = pos[nid][0], pos[nid][1]
            best = [orig_x, orig_y]
            best_c = _incident_crossings(pos, edge_list, incident)

            # Multi-scale radial search: 12 directions × 4 radii.
            # All candidates are offsets from nid's OWN position, so no two
            # nodes can ever land on the same point (which would render an
            # edge between them as a self-loop dot).
            # Large-scale relocation is handled by the swap pass that runs first.
            for r_mult in (0.5, 1.0, 2.0, 3.5):
                r = ring_r * r_mult
                for i in range(12):
                    angle = 2 * math.pi * i / 12
                    pos[nid] = [
                        orig_x + r * math.cos(angle),
                        orig_y + r * math.sin(angle),
                    ]
                    c = _incident_crossings(pos, edge_list, incident)
                    if c < best_c:
                        best_c = c
                        best = [pos[nid][0], pos[nid][1]]

            pos[nid] = best
            if best[0] != orig_x or best[1] != orig_y:
                improved = True

        if not improved:
            break


def _spring_layout(
    node_ids: list[int],
    edges: list[tuple[int, int, int]],
    iterations: int = 500,
    spread: float = 1.0,
    restarts: int = 6,
) -> dict[int, tuple[float, float]]:
    """
    Fruchterman-Reingold spring layout with multiple restarts.

    Selects the best result by crossing count first, then FR energy as a
    tiebreaker.  A greedy node-swap pass is applied to the winner to further
    reduce crossings.

    spread   – multiplier on the ideal inter-node distance (1.0 = default).
    restarts – number of independent attempts; the best is kept (default 6).
    """
    n = len(node_ids)
    if n == 0:
        return {}
    if n == 1:
        return {node_ids[0]: (0.0, 0.0)}

    k = math.sqrt(spread * 5.0 / n)

    weight_map: dict[tuple[int, int], float] = {}
    for u, v, w in edges:
        key = (min(u, v), max(u, v))
        weight_map[key] = weight_map.get(key, 0) + w
    max_w = max(weight_map.values()) if weight_map else 1.0

    edge_pairs: set[tuple[int, int]] = set(weight_map.keys())
    edge_list: list[tuple[int, int]] = list(weight_map.keys())

    def _one_restart(restart: int) -> tuple[dict[int, list[float]], tuple[int, float]]:
        import random as _rand

        # Restart 0 is deterministic (repeatable baseline); the rest use
        # entropy-seeded RNG so each call explores different regions of the
        # search space instead of always revisiting the same fixed seeds.
        rng = _rand.Random(0) if restart == 0 else _rand.Random()
        if restart == 0:
            # First attempt: evenly-spaced circle with small jitter to break
            # the symmetry that causes nodes to lock onto polygon vertices.
            pos: dict[int, list[float]] = {
                nid: [
                    math.cos(2 * math.pi * i / n) + rng.uniform(-0.15, 0.15),
                    math.sin(2 * math.pi * i / n) + rng.uniform(-0.15, 0.15),
                ]
                for i, nid in enumerate(node_ids)
            }
        else:
            # Subsequent attempts: random positions to escape local minima
            pos = {
                nid: [rng.uniform(-1.0, 1.0), rng.uniform(-1.0, 1.0)]
                for nid in node_ids
            }
        _run_fr(node_ids, pos, weight_map, max_w, k, iterations)
        crossings = _count_crossings(pos, edge_list)
        energy = _layout_energy(pos, edge_pairs, node_ids, k)
        return pos, (crossings, energy)

    best_pos: dict[int, list[float]] | None = None
    best_score: tuple[int, float] = (2**31, float("inf"))

    futures = [_layout_executor.submit(_one_restart, r) for r in range(restarts)]
    for fut in as_completed(futures):
        pos, score = fut.result()
        if score < best_score:
            best_score = score
            best_pos = {nid: [pos[nid][0], pos[nid][1]] for nid in node_ids}

    assert best_pos is not None
    _swap_to_reduce_crossings(node_ids, best_pos, edge_list)
    _nudge_to_reduce_crossings(node_ids, best_pos, edge_list)
    return {nid: (best_pos[nid][0], best_pos[nid][1]) for nid in node_ids}


# ---------------------------------------------------------------------------
# Multi-component packing
# ---------------------------------------------------------------------------


def _pack_component_layouts(
    component_layouts: list[tuple[list[int], dict[int, tuple[float, float]]]],
    spread: float = 1.0,
) -> dict[int, tuple[float, float]]:
    """
    Pack several independently laid-out components into the [-1, 1]² canvas.

    Each component is normalised to a unit circle (85th-percentile radius,
    same policy as the single-component path), the spread radial transform is
    applied per-component, then components are packed:

    - 2–3 components: a single row with cell widths ∝ sqrt(node_count) so a
      large main cluster isn't squeezed to the same width as a tiny one.
    - 4+ components: an equal-cell grid.
    """
    # Normalise each component to a unit circle and apply spread transform
    normed: list[tuple[list[int], dict[int, tuple[float, float]]]] = []
    for nodes, raw in component_layouts:
        if len(nodes) == 1:
            normed.append((nodes, {nodes[0]: (0.0, 0.0)}))
            continue
        xs = [raw[nid][0] for nid in nodes]
        ys = [raw[nid][1] for nid in nodes]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        radii = sorted(
            math.sqrt((raw[nid][0] - cx) ** 2 + (raw[nid][1] - cy) ** 2)
            for nid in nodes
        )
        pct_idx = min(len(radii) - 1, max(0, int(0.85 * len(radii))))
        norm_r = radii[pct_idx] or radii[-1] or 1.0
        unit: dict[int, tuple[float, float]] = {}
        for nid in nodes:
            x = (raw[nid][0] - cx) / norm_r
            y = (raw[nid][1] - cy) / norm_r
            if spread != 1.0:
                r = math.sqrt(x * x + y * y)
                if r > 1e-9:
                    s = r ** (1.0 / spread - 1)
                    x, y = x * s, y * s
            unit[nid] = (x, y)
        normed.append((nodes, unit))

    result: dict[int, tuple[float, float]] = {}
    n = len(normed)

    if n == 1:
        # Single component — return normalised positions directly; no cell
        # scaling so the graph fills the full canvas without the 18% shrink
        # that the weighted-row formula would apply.
        result.update(normed[0][1])
        return result

    if n <= 3:
        # Weighted row: cell width ∝ sqrt(node_count)
        weights = [math.sqrt(len(nodes)) for nodes, _ in normed]
        total_w = sum(weights)
        x_cursor = -1.0
        for (_nodes, unit), w in zip(normed, weights):
            cell_w = 2.0 * w / total_w
            cell_cx = x_cursor + cell_w * 0.5
            hw = cell_w * 0.5 * 0.82  # 82% fill — gap between components
            for nid, (x, y) in unit.items():
                result[nid] = (cell_cx + x * hw, y * 0.82)
            x_cursor += cell_w
    else:
        # Equal grid
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        cell_w = 2.0 / cols
        cell_h = 2.0 / rows
        for i, (_nodes, unit) in enumerate(normed):
            col = i % cols
            row = i // cols
            cell_cx = -1.0 + cell_w * (col + 0.5)
            cell_cy = 1.0 - cell_h * (row + 0.5)
            hw = cell_w * 0.5 * 0.80
            hh = cell_h * 0.5 * 0.80
            for nid, (x, y) in unit.items():
                result[nid] = (cell_cx + x * hw, cell_cy + y * hh)

    return result


# ---------------------------------------------------------------------------
# Chart renderer
# ---------------------------------------------------------------------------

_NODE_FOCUS = "#eb459e"  # pink  — focused user
_NODE_SECONDARY = "#57f287"  # green — 2nd-level connections

# Label placement metrics for a 12×12 figure with ±1.5 data-unit axes:
# 1 data unit = 4 in, 1 in = 72 pt, DejaVu Sans glyph ≈ 0.6 em wide.
_LABEL_CW = 0.017  # data-unit width per character at 8 pt
_LABEL_LH = 0.033  # data-unit line height at 8 pt


def _place_labels(
    node_ids: list[int],
    pos_n: dict[int, tuple[float, float]],
    node_size: dict[int, float],
    edges: list[tuple[int, int, int]],
    name_map: dict[int, str],
    font_sizes: dict[int, int],
) -> dict[int, tuple[float, float]]:
    """
    Return {nid: (label_cx, label_cy)} — bounding-box centres for node labels.

    Render text with ha="center", va="center" at these coordinates.

    Algorithm (greedy, hubs-first):
      For each node try 12 angular directions × 3 radii (36 candidates).
      Pick the candidate that points most away from connected neighbours
      while not overlapping any already-placed label bounding box.
    """
    neighbors: dict[int, list[int]] = {nid: [] for nid in node_ids}
    for u, v, _ in edges:
        neighbors[u].append(v)
        neighbors[v].append(u)

    # Half-sizes of each label bounding box.
    lhw: dict[int, float] = {}
    lhh: dict[int, float] = {}
    for nid in node_ids:
        scale = font_sizes[nid] / 8.0
        text = _clean_label(name_map.get(nid, str(nid)))
        lhw[nid] = max(len(text), 1) * _LABEL_CW * scale / 2.0
        lhh[nid] = _LABEL_LH * scale / 2.0

    _DIRS = [
        (math.cos(2 * math.pi * i / 12), math.sin(2 * math.pi * i / 12))
        for i in range(12)
    ]
    _RADII = (1.0, 1.5, 2.2)
    _GAP = 0.005  # minimum clear gap between adjacent label boxes

    # Place most-connected nodes first so hubs get the cleanest spots.
    ordered = sorted(node_ids, key=lambda n: -len(neighbors[n]))
    placed: list[tuple[float, float, float, float]] = []  # (cx, cy, hw, hh)
    label_center: dict[int, tuple[float, float]] = {}

    for nid in ordered:
        x, y = pos_n[nid]
        # Base offset: dot radius + half label height + small gap.
        # With va="center", cy - lhh is the bottom of the text box;
        # placing it at node_r + gap ensures the text clears the dot.
        node_r = math.sqrt(node_size[nid] / math.pi) / 288  # dot radius in data units
        base_pad = node_r + lhh[nid] + 0.015
        nbrs = neighbors[nid]

        best: tuple[float, float] = (x, y + base_pad)
        best_score = -1e9

        for r_mult in _RADII:
            pad = base_pad * r_mult
            for da, db in _DIRS:
                cx, cy = x + da * pad, y + db * pad

                # Prefer directions pointing away from connected neighbours.
                dir_score = (
                    sum(
                        -(da * (pos_n[nb][0] - x) + db * (pos_n[nb][1] - y))
                        / (math.hypot(pos_n[nb][0] - x, pos_n[nb][1] - y) + 1e-9)
                        for nb in nbrs
                    )
                    if nbrs
                    else db  # isolated nodes: prefer upward
                )

                # Heavy per-box penalty for each overlapping placed label.
                overlap_pen = sum(
                    3.0
                    for blx, bly, bhw, bhh in placed
                    if abs(cx - blx) < lhw[nid] + bhw + _GAP
                    and abs(cy - bly) < lhh[nid] + bhh + _GAP
                )

                # Slight preference for shorter radii (labels closer to node).
                score = dir_score - overlap_pen - (r_mult - 1.0) * 0.3

                if score > best_score:
                    best_score = score
                    best = (cx, cy)

        placed.append((best[0], best[1], lhw[nid], lhh[nid]))
        label_center[nid] = best

    return label_center


def _community_clustered_layout(
    node_ids: list[int],
    edges: list[tuple[int, int, int]],
    communities: dict[int, int],
    spread: float = 1.0,
) -> dict[int, tuple[float, float]]:
    """Lay out nodes using community-aware clustering.

    Each community is laid out internally with force-directed placement, then
    communities are arranged around the canvas so clusters are visually
    distinct.  Inter-community edges cross the gaps between clusters.
    """
    # Group nodes by community
    comm_nodes: dict[int, list[int]] = {}
    for nid in node_ids:
        c = communities[nid]
        comm_nodes.setdefault(c, []).append(nid)

    # Sort communities by size (largest first) for a stable layout
    sorted_comms = sorted(comm_nodes.keys(), key=lambda c: -len(comm_nodes[c]))

    # Lay out each community internally
    comm_layouts: dict[int, dict[int, tuple[float, float]]] = {}
    for c in sorted_comms:
        c_nodes = comm_nodes[c]
        c_set = set(c_nodes)
        c_edges = [(u, v, w) for u, v, w in edges if u in c_set and v in c_set]
        if len(c_nodes) == 1:
            comm_layouts[c] = {c_nodes[0]: (0.0, 0.0)}
        elif len(c_nodes) <= 4:
            comm_layouts[c] = _spring_layout(
                c_nodes, c_edges, spread=spread, restarts=2, iterations=150
            )
        else:
            comm_layouts[c] = _spring_layout(c_nodes, c_edges, spread=spread)

    n_comms = len(sorted_comms)
    if n_comms == 1:
        # Single community — use it directly, pack handles normalisation
        c = sorted_comms[0]
        return _pack_component_layouts(
            [(comm_nodes[c], comm_layouts[c])], spread=spread
        )

    # Place community centres on a circle (or line for 2)
    comm_centre: dict[int, tuple[float, float]] = {}
    if n_comms == 2:
        comm_centre[sorted_comms[0]] = (-0.70, 0.0)
        comm_centre[sorted_comms[1]] = (0.70, 0.0)
    else:
        for i, c in enumerate(sorted_comms):
            angle = 2 * math.pi * i / n_comms - math.pi / 2
            r = 0.85
            comm_centre[c] = (r * math.cos(angle), r * math.sin(angle))

    # Determine per-community radius based on relative node count
    total_nodes = len(node_ids)
    comm_radius: dict[int, float] = {}
    for c in sorted_comms:
        frac = len(comm_nodes[c]) / total_nodes
        comm_radius[c] = max(0.20, 0.65 * math.sqrt(frac))

    # Compose final positions: normalise each community's internal layout
    # to fit within its radius, then offset to its centre
    pos: dict[int, tuple[float, float]] = {}
    for c in sorted_comms:
        layout = comm_layouts[c]
        nodes = comm_nodes[c]
        cx, cy = comm_centre[c]
        radius = comm_radius[c]

        if len(nodes) == 1:
            pos[nodes[0]] = (cx, cy)
            continue

        # Find bounding radius of internal layout
        xs = [layout[n][0] for n in nodes]
        ys = [layout[n][1] for n in nodes]
        icx = sum(xs) / len(xs)
        icy = sum(ys) / len(ys)
        max_r = (
            max(
                math.sqrt((layout[n][0] - icx) ** 2 + (layout[n][1] - icy) ** 2)
                for n in nodes
            )
            or 1.0
        )

        scale = radius / max_r
        for nid in nodes:
            x = cx + (layout[nid][0] - icx) * scale
            y = cy + (layout[nid][1] - icy) * scale
            pos[nid] = (x, y)

    return pos


def render_connection_web(
    edges: list[tuple[int, int, int]],
    name_map: dict[int, str],
    guild_name: str,
    focus_user_id: int | None = None,
    second_level_ids: set[int] | None = None,
    spread: float = 1.0,
) -> bytes:
    """
    Render the user interaction network as PNG bytes.

    - Server-wide view (no focus): community-clustered layout with colour-
      coded clusters.
    - Focused-member view: radial ego layout with the focus user at centre
      and connections in concentric rings.
    """
    if not edges:
        raise ValueError("No edges to render.")

    node_ids = list({uid for u, v, _ in edges for uid in (u, v)})

    # ---- Layout: community-clustered for all views ----
    communities = _detect_communities(node_ids, edges)
    pos_n = _community_clustered_layout(node_ids, edges, communities, spread=spread)

    max_weight = max(w for _, _, w in edges)

    fig, ax = plt.subplots(figsize=(12, 12))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    # Node volume for sizing
    node_vol: dict[int, int] = {}
    for u, v, w in edges:
        node_vol[u] = node_vol.get(u, 0) + w
        node_vol[v] = node_vol.get(v, 0) + w
    max_vol = max(node_vol.values()) if node_vol else 1

    # Font sizes and node sizing — nodes must be large enough to contain the name.
    font_sizes: dict[int, float] = {}
    node_size: dict[int, float] = {}
    wrapped_labels: dict[int, str] = {}
    for nid in node_ids:
        label = _wrap_label(name_map.get(nid, str(nid)))
        wrapped_labels[nid] = label
        lines = label.split("\n")
        longest = max(len(ln) for ln in lines)
        n_lines = len(lines)
        # Base font: scale down for longer lines so they fit.
        if nid == focus_user_id:
            fs = max(6.0, 11.0 - 0.3 * max(0, longest - 6))
        else:
            fs = max(5.0, 8.5 - 0.25 * max(0, longest - 6))
        font_sizes[nid] = fs
        # Minimum scatter size to contain the wrapped label.
        # Scatter `s` is area in points²; radius in pts = sqrt(s / π).
        # Text width ≈ longest_line * fs * 0.55; height ≈ n_lines * fs * 1.2.
        text_w = max(longest, 1) * fs * 0.55 + 6
        text_h = n_lines * fs * 1.2 + 4
        # Need circle diameter >= max(text_w, text_h).
        diameter = max(text_w, text_h)
        min_s = math.pi * (diameter / 2) ** 2
        vol_s = (300 if nid == focus_user_id else 120) + 600 * (
            node_vol.get(nid, 1) / max_vol
        )
        node_size[nid] = max(min_s, vol_s)

    # Push apart overlapping nodes — sizes may now exceed the layout spacing.
    # Radius in data units: scatter s is area in pts²; at 130 dpi on a 12 in
    # figure spanning 3 data units, 1 data unit ≈ 520 px ≈ 288 pt.
    node_r: dict[int, float] = {
        nid: math.sqrt(node_size[nid] / math.pi) / 288 for nid in node_ids
    }
    _GAP = 0.008  # small breathing room between circles
    for _pass in range(50):
        moved = False
        for i in range(len(node_ids)):
            u = node_ids[i]
            for j in range(i + 1, len(node_ids)):
                v = node_ids[j]
                dx = pos_n[u][0] - pos_n[v][0]
                dy = pos_n[u][1] - pos_n[v][1]
                d = math.sqrt(dx * dx + dy * dy) or 1e-9
                min_d = node_r[u] + node_r[v] + _GAP
                if d < min_d:
                    overlap = (min_d - d) / 2
                    nx, ny = dx / d, dy / d
                    pos_n[u] = (pos_n[u][0] + nx * overlap, pos_n[u][1] + ny * overlap)
                    pos_n[v] = (pos_n[v][0] - nx * overlap, pos_n[v][1] - ny * overlap)
                    moved = True
        if not moved:
            break

    # Prune inter-community edges: for each node keep only its strongest
    # cross-group connection so the graph isn't overwhelmed with bridge lines.
    if communities is not None:
        # Per node, find the heaviest inter-community edge.
        best_cross: dict[int, int] = {}  # nid -> best weight across communities
        for u, v, w in edges:
            if communities.get(u) != communities.get(v):
                if w > best_cross.get(u, 0):
                    best_cross[u] = w
                if w > best_cross.get(v, 0):
                    best_cross[v] = w

        draw_edges: list[tuple[int, int, int]] = []
        for u, v, w in edges:
            if communities.get(u) == communities.get(v):
                draw_edges.append((u, v, w))
            else:
                # Keep the edge only if it's the top cross-community edge for
                # at least one of its endpoints.
                if w >= best_cross.get(u, 0) or w >= best_cross.get(v, 0):
                    draw_edges.append((u, v, w))
    else:
        draw_edges = list(edges)

    # Draw edges — clipped around every node circle they pass through.
    for u, v, w in draw_edges:
        xu, yu = pos_n[u]
        xv, yv = pos_n[v]
        dx = xv - xu
        dy = yv - yu
        d = math.sqrt(dx * dx + dy * dy) or 1e-9

        # Collect (t_enter, t_exit) intervals where the edge is hidden by a node.
        blocked: list[tuple[float, float]] = []
        for nid in node_ids:
            nx, ny = pos_n[nid]
            r = node_r[nid]
            # Project node centre onto the edge line: t = dot(node-u, v-u) / |v-u|²
            ex, ey = nx - xu, ny - yu
            t_proj = (ex * dx + ey * dy) / (d * d)
            # Perpendicular distance from node centre to the line
            closest_x = xu + dx * t_proj - nx
            closest_y = yu + dy * t_proj - ny
            perp = math.sqrt(closest_x * closest_x + closest_y * closest_y)
            if perp < r:
                # Half-chord length along the edge that falls inside the circle
                half_chord = math.sqrt(r * r - perp * perp) / d
                blocked.append((t_proj - half_chord, t_proj + half_chord))

        # Merge overlapping blocked intervals and compute visible segments.
        blocked.sort()
        merged: list[tuple[float, float]] = []
        for lo, hi in blocked:
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))

        # Build visible segments from the gaps between blocked intervals.
        visible: list[tuple[float, float]] = []
        cursor = 0.0
        for lo, hi in merged:
            if lo > cursor:
                visible.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < 1.0:
            visible.append((cursor, 1.0))

        if not visible:
            continue

        alpha = 0.25 + 0.65 * (w / max_weight)
        lw = 0.8 + 4.0 * (math.log1p(w) / math.log1p(max_weight))
        if communities is not None and communities.get(u) != communities.get(v):
            alpha *= 0.5
            lw *= 0.7

        for t0, t1 in visible:
            ax.plot(
                [xu + dx * t0, xu + dx * t1],
                [yu + dy * t0, yu + dy * t1],
                color=_EDGE_COLOR,
                linewidth=lw,
                alpha=alpha,
                solid_capstyle="round",
                zorder=1,
            )

        if w >= max_weight * 0.15:
            # Place weight label at the midpoint of the longest visible segment.
            longest_seg = max(visible, key=lambda s: s[1] - s[0])
            mt = (longest_seg[0] + longest_seg[1]) / 2
            ax.text(
                xu + dx * mt,
                yu + dy * mt,
                str(w),
                ha="center",
                va="center",
                color=_TEXT,
                fontsize=6,
                alpha=0.7,
                zorder=3,
            )

    # Determine node colour
    def _node_color(nid: int) -> str:
        if nid == focus_user_id:
            return _NODE_FOCUS
        if second_level_ids and nid in second_level_ids:
            return _NODE_SECONDARY
        if communities is not None:
            return _COMMUNITY_COLORS[communities[nid] % len(_COMMUNITY_COLORS)]
        return _NODE

    # Draw nodes and labels — names are rendered inside the dots.
    for nid in node_ids:
        x, y = pos_n[nid]
        is_focus = nid == focus_user_id
        ax.scatter(
            x,
            y,
            s=node_size[nid],
            color=_node_color(nid),
            alpha=0.8,
            zorder=4,
            edgecolors=_TEXT if is_focus else _NODE_EDGE,
            linewidths=1.5 if is_focus else 0.8,
        )
        ax.text(
            x,
            y,
            wrapped_labels[nid],
            ha="center",
            va="center",
            color=_BG,
            fontsize=font_sizes[nid],
            fontweight="bold",
            linespacing=1.2,
            zorder=5,
        )

    ax.set_xlim(-1.5, 1.5)
    ax.set_ylim(-1.5, 1.5)
    ax.set_aspect("equal")
    ax.axis("off")

    if focus_user_id is not None:
        focus_name = name_map.get(focus_user_id, str(focus_user_id))
        title = f"{guild_name} — {focus_name}'s Connections  (replies + mentions)"
    else:
        title = f"{guild_name} — Interaction Web  (replies + mentions)"
    ax.set_title(title, color=_TEXT, fontsize=14, pad=12)

    # Legend
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    legend_elements: list = [
        Line2D(
            [0],
            [0],
            color=_EDGE_COLOR,
            linewidth=1,
            label="few interactions",
            alpha=0.4,
        ),
        Line2D(
            [0],
            [0],
            color=_EDGE_COLOR,
            linewidth=4,
            label="many interactions",
            alpha=0.9,
        ),
    ]
    if focus_user_id is not None:
        legend_elements.append(Patch(color=_NODE_FOCUS, label="focused member"))
    if second_level_ids:
        legend_elements.append(
            Patch(color=_NODE_SECONDARY, label="2nd-level connections")
        )
    if communities is not None:
        n_comms = len(set(communities.values()))
        if n_comms > 1:
            for c in sorted(set(communities.values())):
                color = _COMMUNITY_COLORS[c % len(_COMMUNITY_COLORS)]
                count = sum(1 for v in communities.values() if v == c)
                legend_elements.append(
                    Patch(color=color, label=f"group {c + 1} ({count} members)")
                )
    ax.legend(
        handles=legend_elements,
        facecolor=_BG,
        edgecolor=_GRID,
        labelcolor=_TEXT,
        fontsize=9,
        loc="lower right",
    )

    plt.tight_layout(pad=0.5)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Heatmap renderer
# ---------------------------------------------------------------------------


def render_interaction_heatmap(
    edges: list[tuple[int, int, int]],
    name_map: dict[int, str],
    guild_name: str,
) -> bytes:
    """Render an adjacency-matrix heatmap as PNG bytes.

    Users are ordered by total interaction volume.  Colour intensity maps to
    interaction weight.
    """
    if not edges:
        raise ValueError("No edges to render.")

    # Total volume per user for ordering
    node_vol: dict[int, int] = {}
    for u, v, w in edges:
        node_vol[u] = node_vol.get(u, 0) + w
        node_vol[v] = node_vol.get(v, 0) + w

    # Order by descending volume
    ordered = sorted(node_vol.keys(), key=lambda n: -node_vol[n])
    idx = {nid: i for i, nid in enumerate(ordered)}
    n = len(ordered)

    # Build symmetric weight matrix
    matrix = [[0] * n for _ in range(n)]
    for u, v, w in edges:
        if u in idx and v in idx:
            matrix[idx[u]][idx[v]] = w
            matrix[idx[v]][idx[u]] = w

    labels = [_clean_label(name_map.get(nid, str(nid))) for nid in ordered]

    # Dynamic figure size: scale with member count so labels stay readable
    cell_px = max(0.35, min(0.7, 14.0 / n))
    fig_size = max(8, cell_px * n + 2.5)

    fig, ax = plt.subplots(figsize=(fig_size, fig_size))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap

    data = np.array(matrix, dtype=float)
    # Custom colourmap: dark bg -> blurple
    cmap = LinearSegmentedColormap.from_list(
        "discord", [_BG, "#3a3d8f", _NODE, "#eb459e"], N=256
    )

    im = ax.imshow(data, cmap=cmap, aspect="equal", interpolation="nearest")

    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    font_size = max(5, min(9, int(140 / n)))
    ax.set_xticklabels(labels, rotation=45, ha="right", color=_TEXT, fontsize=font_size)
    ax.set_yticklabels(labels, color=_TEXT, fontsize=font_size)
    ax.tick_params(axis="both", which="both", length=0)

    # Cell value annotations for small matrices
    if n <= 25:
        val_fontsize = max(5, min(8, int(100 / n)))
        for i in range(n):
            for j in range(n):
                v = matrix[i][j]
                if v > 0:
                    ax.text(
                        j,
                        i,
                        str(v),
                        ha="center",
                        va="center",
                        color=_TEXT if v < data.max() * 0.7 else "#000000",
                        fontsize=val_fontsize,
                        fontweight="bold",
                    )

    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.ax.yaxis.set_tick_params(color=_TEXT)
    cbar.ax.set_ylabel("interactions", color=_TEXT, fontsize=10)
    for label in cbar.ax.get_yticklabels():
        label.set_color(_TEXT)

    ax.set_title(
        f"{guild_name} — Interaction Heatmap  (replies + mentions)",
        color=_TEXT,
        fontsize=14,
        pad=12,
    )

    plt.tight_layout(pad=1.0)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
