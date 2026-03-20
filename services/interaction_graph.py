"""Interaction graph — replies and mentions between users.

Stores pairwise interaction weights and renders a spring-layout network chart.
"""
from __future__ import annotations

import io
import math
import sqlite3

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")

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


def record_interactions(
    conn: sqlite3.Connection,
    guild_id: int,
    from_user_id: int,
    to_user_ids: list[int],
    amount: int = 1,
) -> None:
    """Increment the interaction weight from *from_user_id* to each target."""
    for to_user_id in to_user_ids:
        if to_user_id == from_user_id:
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
) -> list[tuple[int, int, int]]:
    """
    Return directed edges as (from_user_id, to_user_id, combined_weight).

    Combined weight merges A→B and B→A into one undirected edge so the
    chart shows total interaction between each pair.

    Restricted to the top *limit_users* by total interaction volume to keep
    the chart readable.
    """
    # Top users by total interaction volume (sender + receiver)
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

def _spring_layout(
    node_ids: list[int],
    edges: list[tuple[int, int, int]],
    iterations: int = 300,
) -> dict[int, tuple[float, float]]:
    """
    Fruchterman-Reingold spring layout.
    Returns a dict of node_id -> (x, y) in roughly [-1, 1]^2.
    """
    n = len(node_ids)
    if n == 0:
        return {}
    if n == 1:
        return {node_ids[0]: (0.0, 0.0)}

    # Start on a circle
    pos: dict[int, list[float]] = {
        nid: [math.cos(2 * math.pi * i / n), math.sin(2 * math.pi * i / n)]
        for i, nid in enumerate(node_ids)
    }

    # Larger k = nodes want to sit further apart.
    # Using area=5 instead of 1 gives roughly 2x the natural spacing.
    k = math.sqrt(5.0 / n)

    # Build symmetric weight map for attractions
    weight_map: dict[tuple[int, int], float] = {}
    for u, v, w in edges:
        key = (min(u, v), max(u, v))
        weight_map[key] = weight_map.get(key, 0) + w
    max_w = max(weight_map.values()) if weight_map else 1.0

    for step in range(iterations):
        # Higher starting temperature lets nodes travel further before cooling
        t = max(0.005, 1.0 * (1 - step / iterations))
        disp: dict[int, list[float]] = {nid: [0.0, 0.0] for nid in node_ids}

        # Repulsion between every pair
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

        # Attraction along edges — scale kept low so repulsion can compete
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

        # Apply displacements with cooling cap
        for nid in node_ids:
            mag = math.sqrt(disp[nid][0] ** 2 + disp[nid][1] ** 2) or 1e-6
            capped = min(mag, t)
            pos[nid][0] += disp[nid][0] / mag * capped
            pos[nid][1] += disp[nid][1] / mag * capped

    return {nid: (pos[nid][0], pos[nid][1]) for nid in node_ids}


# ---------------------------------------------------------------------------
# Chart renderer
# ---------------------------------------------------------------------------

_NODE_FOCUS = "#eb459e"   # pink highlight for the centred user


def render_connection_web(
    edges: list[tuple[int, int, int]],
    name_map: dict[int, str],
    guild_name: str,
    focus_user_id: int | None = None,
) -> bytes:
    """
    Render the user interaction network as PNG bytes.

    edges         – list of (user_id_a, user_id_b, combined_weight)
    name_map      – user_id -> display name
    focus_user_id – when set, this node is rendered in a highlight colour
                    and the title reflects the focused view
    """
    if not edges:
        raise ValueError("No edges to render.")

    node_ids = list({uid for u, v, _ in edges for uid in (u, v)})
    pos = _spring_layout(node_ids, edges)

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
        size = (300 if is_focus else 120) + 600 * (vol / max_vol)
        ax.scatter(
            x, y, s=size,
            color=_NODE_FOCUS if is_focus else _NODE,
            zorder=4,
            edgecolors=_TEXT if is_focus else _NODE_EDGE,
            linewidths=1.5 if is_focus else 0.8,
        )

    # Node labels — offset to avoid overlapping the dot
    label_pad = 0.07
    for nid in node_ids:
        x, y = pos_n[nid]
        is_focus = nid == focus_user_id
        ax.text(
            x, y + label_pad,
            name_map.get(nid, str(nid)),
            ha="center", va="bottom",
            color=_NODE_FOCUS if is_focus else _TEXT,
            fontsize=10 if is_focus else 8,
            fontweight="bold",
            zorder=5,
        )

    ax.set_xlim(-1.4, 1.4)
    ax.set_ylim(-1.4, 1.4)
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
