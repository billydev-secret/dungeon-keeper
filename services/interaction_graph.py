"""Interaction graph — replies and mentions between users.

Stores pairwise interaction weights and renders a spring-layout network chart.
"""
from __future__ import annotations

import io
import math
import re
import sqlite3
import time as _time

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")

# Strip characters that DejaVu Sans (matplotlib default) cannot render,
# primarily emoji and other supplementary-plane glyphs that show as boxes.
_UNRENDERABLE_RE = re.compile(
    "[\U00010000-\U0010FFFF"  # supplementary planes (most emoji)
    "\u2600-\u27BF"           # misc symbols & dingbats
    "\uFE00-\uFE0F"           # variation selectors
    "\u200D\uFEFF]+",         # zero-width joiner, BOM
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
        (u, v, w) for (u, v), w in merged.items() if w >= min_weight
    ]


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


def _spring_layout(
    node_ids: list[int],
    edges: list[tuple[int, int, int]],
    iterations: int = 500,
    spread: float = 1.0,
    restarts: int = 3,
) -> dict[int, tuple[float, float]]:
    """
    Fruchterman-Reingold spring layout with multiple restarts.

    Runs the layout *restarts* times from different random starting positions
    and returns the lowest-energy result, which greatly reduces tangling.

    spread   – multiplier on the ideal inter-node distance (1.0 = default).
    restarts – number of independent attempts; the best is kept (default 3).
    """
    import random as _rng

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

    best_pos: dict[int, list[float]] | None = None
    best_energy = float("inf")

    for restart in range(restarts):
        if restart == 0:
            # First attempt: evenly-spaced circle with small jitter to break
            # the symmetry that causes nodes to lock onto polygon vertices.
            pos: dict[int, list[float]] = {
                nid: [
                    math.cos(2 * math.pi * i / n) + _rng.uniform(-0.15, 0.15),
                    math.sin(2 * math.pi * i / n) + _rng.uniform(-0.15, 0.15),
                ]
                for i, nid in enumerate(node_ids)
            }
        else:
            # Subsequent attempts: random positions to escape local minima
            pos = {
                nid: [_rng.uniform(-1.0, 1.0), _rng.uniform(-1.0, 1.0)]
                for nid in node_ids
            }

        _run_fr(node_ids, pos, weight_map, max_w, k, iterations)

        energy = _layout_energy(pos, edge_pairs, node_ids, k)
        if energy < best_energy:
            best_energy = energy
            best_pos = {nid: [pos[nid][0], pos[nid][1]] for nid in node_ids}

    assert best_pos is not None
    return {nid: (best_pos[nid][0], best_pos[nid][1]) for nid in node_ids}


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
    pos = _spring_layout(node_ids, edges, spread=spread)

    # Normalise positions to fit in [-1, 1]
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    max_r = max(
        math.sqrt((p[0] - cx) ** 2 + (p[1] - cy) ** 2) for p in pos.values()
    ) or 1.0
    pos_n = {
        nid: ((pos[nid][0] - cx) / max_r, (pos[nid][1] - cy) / max_r)
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

    ax.set_xlim(-1.15, 1.15)
    ax.set_ylim(-1.15, 1.15)
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
