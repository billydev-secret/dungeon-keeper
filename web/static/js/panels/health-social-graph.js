import { api } from "../api.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

const COMMUNITY_COLORS = [
  "#E6B84C", "#7F8F3A", "#B36A92", "#9E3B2E", "#B88A2C", "#949ba4",
  "#c9a84c", "#8a6b7d", "#6b7a3a", "#7e3028",
];

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading social graph...</div></div>';
  let animFrame = null;

  async function load() {
    const d = await api("/api/health/social-graph");
    const panel = container.querySelector(".panel");

    const bridgeRows = (d.bridge_users || []).map((b, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${esc(b.user_name || b.user_id)}</td>
        <td>${b.betweenness}%</td>
      </tr>
    `).join("");

    const clusterRows = (d.clusters || []).map((c, i) => `
      <tr><td><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${COMMUNITY_COLORS[i % COMMUNITY_COLORS.length]}"></span> Cluster ${i + 1}</td><td>${c.size}</td></tr>
    `).join("");

    panel.innerHTML = `
      <header>
        <h2>Social Graph</h2>
        <div class="subtitle">${d.node_count} nodes &middot; ${d.edge_count} edges</div>
      </header>

      <details class="panel-about" style="margin:8px 0 14px;">
        <summary style="cursor:pointer; font-size:0.85rem; color:var(--text-muted, #949ba4);">About this report</summary>
        <div style="margin:6px 0 0; padding:10px 14px; background:var(--bg-secondary, #2b2d31); border-radius:6px; font-size:0.85rem; line-height:1.6; color:var(--text-muted, #949ba4);">
          This maps who interacts with whom based on replies and mentions.
          <strong style="color:var(--text-normal, #dbdee1);">Clustering</strong> shows how much people form tight friend groups — too high means cliques, too low means no real bonds.
          <strong style="color:var(--text-normal, #dbdee1);">Density</strong> is the fraction of all possible connections that actually exist.
          <strong style="color:var(--text-normal, #dbdee1);">Reciprocity</strong> measures two-way conversations — high means people talk <em>with</em> each other, not just <em>at</em> each other.
          <strong style="color:var(--text-normal, #dbdee1);">Bridge users</strong> connect otherwise separate groups and keep the community from fracturing into silos.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Clustering</div>
          <div class="home-card-big">${d.clustering_coefficient}</div>
          <div class="home-card-sub">Friend-group tightness (target: 0.25–0.55)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Density</div>
          <div class="home-card-big">${d.network_density}</div>
          <div class="home-card-sub">How tightly connected everyone is (0–1)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Reciprocity</div>
          <div class="home-card-big">${d.reciprocity}</div>
          <div class="home-card-sub">How often interactions go both ways (target: &gt;0.35)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Bridge Users</div>
          <div class="home-card-big">${d.bridge_count}</div>
          <div class="home-card-sub">${d.isolates} isolates</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">SFW/NSFW Bridge</div>
          <div class="home-card-big">${d.sfw_nsfw_bridge_pct}%</div>
          <div class="home-card-sub">Members active in both</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Network Visualization</div>
        <canvas id="social-graph-canvas" width="800" height="500" style="width:100%;background:var(--bg);border-radius:6px;"></canvas>
      </div>

      <div class="home-grid" style="margin-top:14px;">
        <div class="home-card">
          <div class="home-card-label">Top Bridge Users</div>
          <table class="data-table">
            <thead><tr><th>#</th><th>User</th><th>Betweenness</th></tr></thead>
            <tbody>${bridgeRows}</tbody>
          </table>
        </div>
        <div class="home-card">
          <div class="home-card-label">Detected Clusters</div>
          <table class="data-table">
            <thead><tr><th>Cluster</th><th>Size</th></tr></thead>
            <tbody>${clusterRows}</tbody>
          </table>
        </div>
      </div>
    `;

    // Force-directed graph on canvas
    const canvas = panel.querySelector("#social-graph-canvas");
    if (canvas && d.graph_nodes && d.graph_nodes.length) {
      renderForceGraph(canvas, d.graph_nodes, d.graph_edges);
    }
  }

  function renderForceGraph(canvas, nodes, edges) {
    const ctx2d = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;

    // Initialize positions randomly
    const pos = {};
    nodes.forEach(n => {
      pos[n.id] = { x: Math.random() * W, y: Math.random() * H, vx: 0, vy: 0 };
    });

    const edgeMap = edges.map(e => ({
      source: pos[e.source], target: pos[e.target],
      sourceId: e.source, targetId: e.target, weight: e.weight,
    })).filter(e => e.source && e.target);

    let iterations = 0;
    const maxIter = 150;

    function simulate() {
      if (iterations >= maxIter) {
        draw();
        return;
      }
      iterations++;
      const alpha = 1 - iterations / maxIter;

      // Repulsion
      const nodeArr = Object.values(pos);
      for (let i = 0; i < nodeArr.length; i++) {
        for (let j = i + 1; j < nodeArr.length; j++) {
          const a = nodeArr[i], b = nodeArr[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const force = 800 / (dist * dist) * alpha;
          dx *= force / dist; dy *= force / dist;
          a.vx += dx; a.vy += dy;
          b.vx -= dx; b.vy -= dy;
        }
      }

      // Attraction (edges)
      for (const e of edgeMap) {
        const dx = e.target.x - e.source.x;
        const dy = e.target.y - e.source.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = dist * 0.005 * alpha;
        e.source.vx += dx * force; e.source.vy += dy * force;
        e.target.vx -= dx * force; e.target.vy -= dy * force;
      }

      // Center gravity
      for (const p of nodeArr) {
        p.vx += (W / 2 - p.x) * 0.001 * alpha;
        p.vy += (H / 2 - p.y) * 0.001 * alpha;
        p.x += p.vx; p.y += p.vy;
        p.vx *= 0.8; p.vy *= 0.8;
        p.x = Math.max(20, Math.min(W - 20, p.x));
        p.y = Math.max(20, Math.min(H - 20, p.y));
      }

      draw();
      animFrame = requestAnimationFrame(simulate);
    }

    function draw() {
      ctx2d.clearRect(0, 0, W, H);

      // Edges
      ctx2d.strokeStyle = "rgba(230,184,76,0.12)";
      ctx2d.lineWidth = 1;
      for (const e of edgeMap) {
        ctx2d.beginPath();
        ctx2d.moveTo(e.source.x, e.source.y);
        ctx2d.lineTo(e.target.x, e.target.y);
        ctx2d.stroke();
      }

      // Nodes
      for (const n of nodes) {
        const p = pos[n.id];
        if (!p) continue;
        const r = Math.max(3, Math.min(12, Math.sqrt(n.degree) * 2));
        const color = COMMUNITY_COLORS[n.cluster % COMMUNITY_COLORS.length];
        ctx2d.beginPath();
        ctx2d.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx2d.fillStyle = color;
        ctx2d.fill();
      }
    }

    simulate();
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return {
    unmount() { if (animFrame) cancelAnimationFrame(animFrame); },
  };
}
