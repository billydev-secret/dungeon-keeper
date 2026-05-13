"""Shared graph-theoretic metrics for social/interaction networks.

Takes raw directed edge rows ``(from_user_id, to_user_id, weight)`` and computes
clustering, density, reciprocity, isolates, betweenness-based bridge users,
label-propagation communities, average shortest-path length, the small-world
quotient, and a cross-cluster interaction matrix.

Kept SQLite-free so both the Health dashboard (``compute_social_graph``) and
the Reports Connection Graph endpoint can call it against pre-fetched rows.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable

EdgeRow = tuple[int, int, float]


def _badge(value: float, thresholds: list[tuple[float, str]]) -> str:
    for threshold, label in thresholds:
        if value <= threshold:
            return label
    return thresholds[-1][1] if thresholds else "unknown"


CLUSTERING_BADGE_THRESHOLDS: list[tuple[float, str]] = [
    (0.15, "critical"),
    (0.25, "needs_work"),
    (0.55, "healthy"),
    (1.0, "excellent"),
]


def compute_graph_metrics(
    edge_rows: Iterable[EdgeRow],
    *,
    top_n: int = 100,
    betweenness_sample_cap: int = 200,
    cluster_matrix_cap: int = 8,
    clustering_resolution: float = 1.2,
) -> dict:
    """Compute all social-graph metrics from directed weighted edges.

    edge_rows: iterable of (from_user_id, to_user_id, weight) tuples.
    top_n: cap the returned ``graph_nodes``/``graph_edges`` to the top-N most
           connected nodes so the payload stays reasonable for rendering.
    clustering_resolution: granularity knob for label propagation. 1.0 is
           modularity-neutral; higher values break up large communities.
    """
    out_edges: dict[int, dict[int, float]] = defaultdict(dict)
    in_edges: dict[int, dict[int, float]] = defaultdict(dict)
    all_nodes: set[int] = set()
    for u, v, w in edge_rows:
        if u == v:
            continue
        out_edges[u][v] = out_edges[u].get(v, 0) + w
        in_edges[v][u] = in_edges[v].get(u, 0) + w
        all_nodes.add(u)
        all_nodes.add(v)

    n_nodes = len(all_nodes)
    n_edges = sum(len(vs) for vs in out_edges.values())

    neighbors: dict[int, set[int]] = defaultdict(set)
    for u, vs in out_edges.items():
        for v in vs:
            neighbors[u].add(v)
            neighbors[v].add(u)

    # Clustering coefficient (average local)
    cc_vals: list[float] = []
    for node in all_nodes:
        nbrs = neighbors[node]
        k = len(nbrs)
        if k < 2:
            continue
        triangles = 0
        nbrs_list = list(nbrs)
        for i in range(len(nbrs_list)):
            for j in range(i + 1, len(nbrs_list)):
                if nbrs_list[j] in neighbors[nbrs_list[i]]:
                    triangles += 1
        possible = k * (k - 1) / 2
        cc_vals.append(triangles / possible if possible else 0)
    clustering_coeff = round(statistics.mean(cc_vals), 3) if cc_vals else 0.0

    # Density
    possible_edges = n_nodes * (n_nodes - 1) if n_nodes > 1 else 1
    density = round(n_edges / possible_edges, 4) if possible_edges else 0.0

    # Reciprocity
    reciprocal_count = 0
    for u, vs in out_edges.items():
        for v in vs:
            if u in out_edges.get(v, {}):
                reciprocal_count += 1
    reciprocity = round(reciprocal_count / n_edges, 3) if n_edges else 0.0

    # Isolates
    isolates = sum(1 for node in all_nodes if len(neighbors[node]) <= 1)

    # Betweenness (sampled) + average path length (from the same BFS sweep)
    betweenness: dict[int, float] = defaultdict(float)
    node_list = list(all_nodes)
    sample_nodes = (
        node_list[:betweenness_sample_cap]
        if len(node_list) > betweenness_sample_cap
        else node_list
    )
    path_len_sum = 0.0
    path_len_count = 0

    for source in sample_nodes:
        dist: dict[int, int] = {source: 0}
        pred: dict[int, list[int]] = defaultdict(list)
        queue = [source]
        order: list[int] = []
        idx = 0
        while idx < len(queue):
            u = queue[idx]
            idx += 1
            order.append(u)
            for v in neighbors[u]:
                if v not in dist:
                    dist[v] = dist[u] + 1
                    queue.append(v)
                if dist.get(v) == dist[u] + 1:
                    pred[v].append(u)

        for v, d in dist.items():
            if v == source:
                continue
            path_len_sum += d
            path_len_count += 1

        sigma: dict[int, float] = defaultdict(float)
        sigma[source] = 1.0
        for v in order:
            for p in pred[v]:
                sigma[v] += sigma[p]

        delta: dict[int, float] = defaultdict(float)
        for v in reversed(order):
            for p in pred[v]:
                if sigma[v] > 0:
                    delta[p] += (sigma[p] / sigma[v]) * (1 + delta[v])
            if v != source:
                betweenness[v] += delta[v]

    total_betweenness = sum(betweenness.values()) or 1
    bridge_list: list[dict[str, str | float]] = [
        {"user_id": str(uid), "betweenness": round(b / total_betweenness * 100, 2)}
        for uid, b in betweenness.items()
    ]
    bridge_list.sort(key=lambda x: float(x["betweenness"]), reverse=True)
    bridge_users = bridge_list[:20]
    bridge_count = sum(1 for b in bridge_users if float(b["betweenness"]) > 5)
    top_betweenness_pct = float(bridge_users[0]["betweenness"]) if bridge_users else 0.0

    avg_path_length = (
        round(path_len_sum / path_len_count, 2) if path_len_count else 0.0
    )
    small_world = (
        round(clustering_coeff / density, 2) if density else 0.0
    )

    # Label-propagation community detection — weighted + modularity-style
    # resolution penalty so dense graphs don't collapse to one big cluster.
    adj_w: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for u, vs in out_edges.items():
        for v, w in vs.items():
            adj_w[u][v] += w
            adj_w[v][u] += w
    node_strength: dict[int, float] = {
        n: sum(adj_w[n].values()) for n in all_nodes
    }
    two_m = sum(node_strength.values()) or 1.0

    labels: dict[int, int] = {node: i for i, node in enumerate(all_nodes)}
    comm_strength: dict[int, float] = dict(node_strength)  # label -> total strength
    for _ in range(20):
        changed = False
        for node in all_nodes:
            if not neighbors[node]:
                continue
            # Weighted vote per candidate label.
            counts: dict[int, float] = defaultdict(float)
            for nb in neighbors[node]:
                counts[labels[nb]] += adj_w[node][nb]
            # Subtract expected weight (modularity-style) with resolution gamma.
            # Node's own strength is in its current community — exclude when
            # considering alternatives, include only when weighing a move.
            own_lbl = labels[node]
            own_strength = node_strength[node]
            best_lbl = own_lbl
            best_score = -float("inf")
            for lbl, w_to_lbl in counts.items():
                comm_s = comm_strength.get(lbl, 0.0)
                if lbl == own_lbl:
                    comm_s -= own_strength  # don't penalize self-overlap
                expected = clustering_resolution * own_strength * comm_s / two_m
                score = w_to_lbl - expected
                if score > best_score:
                    best_score = score
                    best_lbl = lbl
            if best_lbl != own_lbl:
                comm_strength[own_lbl] = comm_strength.get(own_lbl, 0.0) - own_strength
                comm_strength[best_lbl] = comm_strength.get(best_lbl, 0.0) + own_strength
                labels[node] = best_lbl
                changed = True
        if not changed:
            break

    # Remap raw labels to dense 0..K-1 sorted by community size (largest = 0)
    raw_groups: dict[int, list[int]] = defaultdict(list)
    for node, lbl in labels.items():
        raw_groups[lbl].append(node)
    ordered = sorted(raw_groups.items(), key=lambda kv: -len(kv[1]))
    label_remap: dict[int, int] = {raw: i for i, (raw, _) in enumerate(ordered)}
    node_cluster: dict[int, int] = {
        node: label_remap[lbl] for node, lbl in labels.items()
    }
    cluster_info = [
        {"id": i, "size": len(members)} for i, (_, members) in enumerate(ordered)
    ][:10]

    # Cross-cluster interaction matrix (cap clusters, bin overflow into "Other")
    if ordered:
        kept = min(cluster_matrix_cap, len(ordered))

        def bin_of(node: int) -> int:
            c = node_cluster.get(node, 0)
            return c if c < kept else kept

        matrix_dim = kept + (1 if len(ordered) > kept else 0)
        matrix = [[0.0 for _ in range(matrix_dim)] for _ in range(matrix_dim)]
        for u, vs in out_edges.items():
            bu = bin_of(u)
            for v, w in vs.items():
                bv = bin_of(v)
                matrix[bu][bv] += w
        cross_cluster_labels = [f"Cluster {i + 1}" for i in range(kept)]
        if len(ordered) > kept:
            cross_cluster_labels.append("Other")
    else:
        matrix = []
        cross_cluster_labels = []

    # Top-N nodes for visualization (by undirected degree)
    top_nodes = sorted(all_nodes, key=lambda u: len(neighbors[u]), reverse=True)[:top_n]
    top_set = set(top_nodes)
    graph_nodes = [
        {
            "id": str(n),
            "degree": len(neighbors[n]),
            "cluster": node_cluster.get(n, 0),
            "out_degree": sum(out_edges.get(n, {}).values()),
            "in_degree": sum(in_edges.get(n, {}).values()),
        }
        for n in top_nodes
    ]
    graph_edges = [
        {"source": str(u), "target": str(v), "weight": w}
        for u, vs in out_edges.items()
        for v, w in vs.items()
        if u in top_set and v in top_set
    ]

    badge = _badge(clustering_coeff, CLUSTERING_BADGE_THRESHOLDS)

    return {
        "clustering_coefficient": clustering_coeff,
        "network_density": density,
        "reciprocity": reciprocity,
        "isolates": isolates,
        "bridge_count": bridge_count,
        "bridge_users": bridge_users[:10],
        "top_betweenness_pct": top_betweenness_pct,
        "clusters": cluster_info,
        "node_cluster": {str(k): v for k, v in node_cluster.items()},
        "cross_cluster_matrix": matrix,
        "cross_cluster_labels": cross_cluster_labels,
        "avg_path_length": avg_path_length,
        "small_world_quotient": small_world,
        "node_count": n_nodes,
        "edge_count": n_edges,
        "badge": badge,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
    }
