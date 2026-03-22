"""Interaction graph — replies and mentions between users.

Stores pairwise interaction weights and renders a spring-layout network chart.
"""
from __future__ import annotations

import io
import math
import re
import sqlite3
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")

# Strip characters that DejaVu Sans (matplotlib default) cannot render.
# DejaVu Sans covers the Basic Latin + Latin-1 Supplement blocks reliably.
# Anything outside U+0020–U+024F is a candidate for box rendering or a
# freetype crash, so we allow only that range plus common punctuation.
_UNRENDERABLE_RE = re.compile(
    "["
    "\U00010000-\U0010FFFF"  # supplementary planes (emoji, etc.)
    "\u0250-\u2DFF"          # extended Latin and everything up to CJK
    "\u2E00-\uFDFF"          # misc punctuation through Arabic
    "\uFE00-\uFFFF"          # variation selectors, specials
    "]+",
    flags=re.UNICODE,
)


def _clean_label(name: str) -> str:
    cleaned = _UNRENDERABLE_RE.sub("", name).strip()
    return cleaned or name  # keep original if everything was stripped


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
        top_rows = conn.execute(
            """
            SELECT user_id, SUM(w) AS total FROM (
                SELECT from_user_id AS user_id, COUNT(*) AS w
                FROM user_interactions_log WHERE guild_id = ? AND ts >= ?
                GROUP BY from_user_id
                UNION ALL
                SELECT to_user_id AS user_id, COUNT(*) AS w
                FROM user_interactions_log WHERE guild_id = ? AND ts >= ?
                GROUP BY to_user_id
            )
            GROUP BY user_id
            ORDER BY total DESC
            LIMIT ?
            """,
            (guild_id, after_ts, guild_id, after_ts, limit_users),
        ).fetchall()
        top_ids = {int(r[0]) for r in top_rows}

        rows = conn.execute(
            """
            SELECT from_user_id, to_user_id, COUNT(*) AS weight
            FROM user_interactions_log
            WHERE guild_id = ? AND ts >= ?
            GROUP BY from_user_id, to_user_id
            ORDER BY weight DESC
            """,
            (guild_id, after_ts),
        ).fetchall()
    else:
        top_rows = conn.execute(
            """
            SELECT user_id, SUM(w) AS total FROM (
                SELECT from_user_id AS user_id, SUM(weight) AS w
                FROM user_interactions WHERE guild_id = ?
                GROUP BY from_user_id
                UNION ALL
                SELECT to_user_id AS user_id, SUM(weight) AS w
                FROM user_interactions WHERE guild_id = ?
                GROUP BY to_user_id
            )
            GROUP BY user_id
            ORDER BY total DESC
            LIMIT ?
            """,
            (guild_id, guild_id, limit_users),
        ).fetchall()
        top_ids = {int(r[0]) for r in top_rows}

        rows = conn.execute(
            """
            SELECT from_user_id, to_user_id, weight
            FROM user_interactions
            WHERE guild_id = ?
            ORDER BY weight DESC
            """,
            (guild_id,),
        ).fetchall()

    # Merge A→B and B→A into a single undirected edge
    merged: dict[tuple[int, int], int] = {}
    for r in rows:
        u, v, w = int(r[0]), int(r[1]), int(r[2])
        if u not in top_ids or v not in top_ids:
            continue
        key = (min(u, v), max(u, v))
        merged[key] = merged.get(key, 0) + w

    return [
        (u, v, w) for (u, v), w in merged.items() if w >= min_weight and u != v
    ]


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
        t = max(0.005, 1.0 * (1 - step / iterations))
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
            scale = 0.15 + 0.35 * (ew / max_w)
            dx = pos[eu][0] - pos[ev][0]
            dy = pos[eu][1] - pos[ev][1]
            d = math.sqrt(dx * dx + dy * dy) or 1e-6
            attr = (d * d / k) * scale
            disp[eu][0] -= attr * dx / d
            disp[eu][1] -= attr * dy / d
            disp[ev][0] += attr * dx / d
            disp[ev][1] += attr * dy / d

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
                energy += (d - k) ** 2      # edge: penalise deviation from k
            else:
                energy += k * k / d         # non-edge: penalise closeness
    return energy


# ---------------------------------------------------------------------------
# Crossing minimization helpers
# ---------------------------------------------------------------------------

def _segments_cross(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, dx: float, dy: float,
) -> bool:
    """Return True if segment AB properly crosses segment CD."""
    def _side(ox: float, oy: float, px: float, py: float, qx: float, qy: float) -> float:
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
            if _segments_cross(ax, ay, bx, by, pos[u2][0], pos[u2][1], pos[v2][0], pos[v2][1]):
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
            if _segments_cross(ax, ay, bx, by, pos[u2][0], pos[u2][1], pos[v2][0], pos[v2][1]):
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

    for _ in range(max_passes):
        improved = False
        for i in range(n):
            for j in range(i + 1, n):
                u, v = node_ids[i], node_ids[j]
                uv = {u, v}
                incident = [
                    k for k in set(adj[u]) | set(adj[v])
                    if set(edge_list[k]) != uv  # skip the invariant u-v edge
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
    One-sided position search run after _swap_to_reduce_crossings.

    For each node, evaluate:
      - every other node's current position (one-sided move — the other
        node stays put, unlike a swap)
      - 8 points on a ring of radius ≈ average inter-node spacing

    The best position (fewest incident crossings) is kept.  One-sided moves
    can resolve crossings that pure swaps cannot: if moving A to B's position
    helps A but worsens B, the swap pass rejects it; this pass accepts it
    and lets B find its own improvement in the same or next iteration.
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

            # One-sided move to each other node's current position
            for other in node_ids:
                if other == nid:
                    continue
                pos[nid] = [pos[other][0], pos[other][1]]
                c = _incident_crossings(pos, edge_list, incident)
                if c < best_c:
                    best_c = c
                    best = [pos[nid][0], pos[nid][1]]

            # Fine-grained: 8 radial offsets around the original position
            for i in range(8):
                angle = math.pi * i / 4
                pos[nid] = [orig_x + ring_r * math.cos(angle),
                            orig_y + ring_r * math.sin(angle)]
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
        # Each restart gets its own seeded RNG — the module-level random
        # instance is not thread-safe.
        import random as _rand
        rng = _rand.Random(restart)
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
    best_score: tuple[int, float] = (2 ** 31, float("inf"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one_restart, r) for r in range(restarts)]
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

    if n <= 3:
        # Weighted row: cell width ∝ sqrt(node_count)
        weights = [math.sqrt(len(nodes)) for nodes, _ in normed]
        total_w = sum(weights)
        x_cursor = -1.0
        for (nodes, unit), w in zip(normed, weights):
            cell_w = 2.0 * w / total_w
            cell_cx = x_cursor + cell_w * 0.5
            hw = cell_w * 0.5 * 0.82   # 82% fill — gap between components
            for nid, (x, y) in unit.items():
                result[nid] = (cell_cx + x * hw, y * 0.82)
            x_cursor += cell_w
    else:
        # Equal grid
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        cell_w = 2.0 / cols
        cell_h = 2.0 / rows
        for i, (nodes, unit) in enumerate(normed):
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

_NODE_FOCUS = "#eb459e"      # pink  — focused user
_NODE_SECONDARY = "#57f287"  # green — 2nd-level connections


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

    edges             – list of (user_id_a, user_id_b, combined_weight)
    name_map          – user_id -> display name
    focus_user_id     – when set, this node is rendered in pink and the
                        title reflects the focused view
    second_level_ids  – nodes that are connections-of-connections; rendered
                        in green to distinguish them from direct connections
    spread            – passed to _spring_layout; controls how far apart nodes sit
    """
    if not edges:
        raise ValueError("No edges to render.")

    node_ids = list({uid for u, v, _ in edges for uid in (u, v)})
    components = _find_components(node_ids, [(u, v) for u, v, _ in edges])

    if len(components) == 1:
        pos = _spring_layout(node_ids, edges, spread=spread)

        # Normalise — 85th-percentile radius keeps the core spread across the
        # canvas; a handful of outlier nodes may sit slightly beyond ±1.
        xs = [pos[nid][0] for nid in node_ids]
        ys = [pos[nid][1] for nid in node_ids]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        radii = sorted(
            math.sqrt((pos[nid][0] - cx) ** 2 + (pos[nid][1] - cy) ** 2)
            for nid in node_ids
        )
        pct_idx = min(len(radii) - 1, max(0, int(0.85 * len(radii))))
        norm_r = radii[pct_idx] or radii[-1] or 1.0
        pos_n: dict[int, tuple[float, float]] = {
            nid: ((pos[nid][0] - cx) / norm_r, (pos[nid][1] - cy) / norm_r)
            for nid in node_ids
        }

        # Radial spread transform: r → r^(1/spread) pushes interior nodes outward.
        # spread=1 is identity; spread=2 moves r=0.25 → 0.5, r=0.5 → 0.707, r=1 → 1.
        if spread != 1.0:
            spread_exp = 1.0 / spread
            new_pos_n: dict[int, tuple[float, float]] = {}
            for nid in node_ids:
                x, y = pos_n[nid]
                r = math.sqrt(x * x + y * y)
                if r > 1e-9:
                    scale = r ** (spread_exp - 1)
                    new_pos_n[nid] = (x * scale, y * scale)
                else:
                    new_pos_n[nid] = (x, y)
            pos_n = new_pos_n
    else:
        # Multiple disconnected components: lay out each independently so the
        # inter-component distance doesn't dominate the normalization scale and
        # squish every group's internal layout.
        components.sort(key=len, reverse=True)
        comp_layouts: list[tuple[list[int], dict[int, tuple[float, float]]]] = []
        for comp_nodes in components:
            comp_set = set(comp_nodes)
            comp_edges = [(u, v, w) for u, v, w in edges if u in comp_set]
            n_comp = len(comp_nodes)
            if n_comp == 1:
                comp_pos: dict[int, tuple[float, float]] = {comp_nodes[0]: (0.0, 0.0)}
            elif n_comp <= 4:
                comp_pos = _spring_layout(
                    comp_nodes, comp_edges, spread=spread, restarts=2, iterations=150
                )
            else:
                comp_pos = _spring_layout(comp_nodes, comp_edges, spread=spread)
            comp_layouts.append((comp_nodes, comp_pos))
        pos_n = _pack_component_layouts(comp_layouts, spread=spread)

    max_weight = max(w for _, _, w in edges)

    fig, ax = plt.subplots(figsize=(12, 12))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    # Draw edges
    for u, v, w in edges:
        xu, yu = pos_n[u]
        xv, yv = pos_n[v]
        alpha = 0.25 + 0.65 * (w / max_weight)
        lw = 0.8 + 4.0 * (math.log1p(w) / math.log1p(max_weight))
        ax.plot(
            [xu, xv], [yu, yv],
            color=_EDGE_COLOR,
            linewidth=lw,
            alpha=alpha,
            solid_capstyle="round",
            zorder=1,
        )
        # Weight label at midpoint for strong connections
        if w >= max_weight * 0.15:
            mx, my = (xu + xv) / 2, (yu + yv) / 2
            ax.text(
                mx, my, str(w),
                ha="center", va="center",
                color=_TEXT, fontsize=6, alpha=0.7,
                zorder=3,
            )

    # Node volume for sizing
    node_vol: dict[int, int] = {}
    for u, v, w in edges:
        node_vol[u] = node_vol.get(u, 0) + w
        node_vol[v] = node_vol.get(v, 0) + w
    max_vol = max(node_vol.values()) if node_vol else 1

    # Draw nodes
    for nid in node_ids:
        x, y = pos_n[nid]
        vol = node_vol.get(nid, 1)
        is_focus = nid == focus_user_id
        is_secondary = bool(second_level_ids and nid in second_level_ids)
        if is_focus:
            node_color = _NODE_FOCUS
        elif is_secondary:
            node_color = _NODE_SECONDARY
        else:
            node_color = _NODE
        size = (300 if is_focus else 120) + 600 * (vol / max_vol)
        ax.scatter(
            x, y, s=size,
            color=node_color,
            zorder=4,
            edgecolors=_TEXT if is_focus else _NODE_EDGE,
            linewidths=1.5 if is_focus else 0.8,
        )

    # Node labels — offset to avoid overlapping the dot
    label_pad = 0.07
    for nid in node_ids:
        x, y = pos_n[nid]
        is_focus = nid == focus_user_id
        is_secondary = bool(second_level_ids and nid in second_level_ids)
        if is_focus:
            label_color = _NODE_FOCUS
        elif is_secondary:
            label_color = _NODE_SECONDARY
        else:
            label_color = _TEXT
        ax.text(
            x, y + label_pad,
            _clean_label(name_map.get(nid, str(nid))),
            ha="center", va="bottom",
            color=label_color,
            fontsize=10 if is_focus else (7 if is_secondary else 8),
            fontweight="bold",
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
        Line2D([0], [0], color=_EDGE_COLOR, linewidth=1, label="few interactions", alpha=0.4),
        Line2D([0], [0], color=_EDGE_COLOR, linewidth=4, label="many interactions", alpha=0.9),
    ]
    if focus_user_id is not None:
        legend_elements.append(Patch(color=_NODE_FOCUS, label="focused member"))
    if second_level_ids:
        legend_elements.append(Patch(color=_NODE_SECONDARY, label="2nd-level connections"))
    ax.legend(
        handles=legend_elements,
        facecolor=_BG, edgecolor=_GRID, labelcolor=_TEXT,
        fontsize=9, loc="lower right",
    )

    plt.tight_layout(pad=0.5)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
