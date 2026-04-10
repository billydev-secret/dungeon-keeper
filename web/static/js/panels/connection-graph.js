import { api } from "../api.js";

// Force-directed network graph rendered on a <canvas>.
// Mirrors /connection_web command options.

const BG       = "#2b2d31";
const NODE_CLR = "#E6B84C";
const NODE_2ND = "#7F8F3A";
const FOCUS_CLR = "#B36A92";
const EDGE_CLR = "rgba(230,184,76,0.25)";
const TEXT_CLR = "#dbdee1";
const HIGHLIGHT = "#B36A92";

const COMMUNITY_COLORS = [
  "#E6B84C", "#7F8F3A", "#B36A92", "#9E3B2E", "#B88A2C", "#949ba4",
  "#c9a84c", "#8a6b7d", "#6b7a3a", "#7e3028", "#d4a84c", "#a89470",
];

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function filterSelect(placeholder, options) {
  const wrap = document.createElement("div");
  wrap.className = "filter-select";
  const input = document.createElement("input");
  input.type = "text"; input.placeholder = placeholder; input.className = "filter-select-input";
  wrap.appendChild(input);
  const list = document.createElement("div");
  list.className = "filter-select-list";
  wrap.appendChild(list);
  let selectedId = "", selectedLabel = "";
  function render(filter) {
    const lc = filter.toLowerCase();
    const matches = lc ? options.filter((o) => o.label.toLowerCase().includes(lc)) : options;
    list.innerHTML = `<div class="filter-select-item" data-id=""><em style="color:var(--text-dim)">(none)</em></div>` +
      matches.slice(0, 80).map((o) => `<div class="filter-select-item" data-id="${esc(o.id)}">${esc(o.label)}</div>`).join("");
  }
  input.addEventListener("focus", () => { render(input.value); list.style.display = "block"; });
  input.addEventListener("input", () => { selectedId = ""; selectedLabel = ""; render(input.value); list.style.display = "block"; });
  list.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".filter-select-item"); if (!item) return;
    selectedId = item.dataset.id; selectedLabel = selectedId ? item.textContent : ""; input.value = selectedLabel; list.style.display = "none";
  });
  input.addEventListener("blur", () => { setTimeout(() => { list.style.display = "none"; }, 150); });
  return { el: wrap, get id() { return selectedId; }, set id(v) { selectedId = v; const o = options.find((x) => x.id === v); input.value = o ? o.label : ""; } };
}

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel" style="display:flex; flex-direction:column; height:calc(100vh - 40px);">
      <header>
        <h2>Connection Graph</h2>
        <div class="subtitle">Visual network of who interacts with whom</div>
      </header>
      <div class="controls" style="flex-wrap:wrap;">
        <label>Layout
          <select data-control="layout">
            <option value="force">Force-directed</option>
            <option value="community">Community clusters</option>
            <option value="radial">Radial</option>
            <option value="circular">Circular</option>
            <option value="hierarchical">Hierarchical</option>
          </select>
        </label>
        <label>Period
          <select data-control="timescale">
            <option value="">All time</option>
            <option value="1">Last 24h</option>
            <option value="7">Last 7 days</option>
            <option value="30">Last 30 days</option>
            <option value="90">Last 90 days</option>
          </select>
        </label>
        <label>Focus member <span data-slot="member" style="display:inline-block;vertical-align:middle;"></span></label>
        <label>Min edge %
          <input type="number" data-control="min_pct" min="0" max="100" value="${initialParams.min_pct || 5}" title="Hide edges below this % of either node's total" />
        </label>
        <label>Layers
          <input type="number" data-control="layers" min="1" max="5" value="${initialParams.layers || 2}" title="Expansion depth from focused member" />
        </label>
        <label>Max nodes
          <input type="number" data-control="limit" min="5" max="100" value="${initialParams.limit || 40}" />
        </label>
        <label>Spread
          <input type="range" data-control="spread" min="0.5" max="3" step="0.1" value="${initialParams.spread || 1.0}" style="width:80px;" />
        </label>
        <label>Max edges/node
          <input type="number" data-control="max_per_node" min="0" max="20" value="${initialParams.max_per_node || 0}" title="0 = no limit" />
        </label>
      </div>
      <div data-graph-wrap style="position:relative; flex:1; min-height:300px; min-width:0; background:${BG}; border-radius:8px; overflow:hidden; cursor:grab;">
        <canvas data-graph></canvas>
      </div>
      <div data-legend style="margin-top:4px; font-size:11px; color:#949ba4;">
        Drag nodes · Scroll to zoom · Pan background · Node size = interactions · Edge width = weight
      </div>
    </div>
  `;

  const layoutEl      = container.querySelector('[data-control="layout"]');
  const timescaleEl   = container.querySelector('[data-control="timescale"]');
  const minPctEl      = container.querySelector('[data-control="min_pct"]');
  const layersEl      = container.querySelector('[data-control="layers"]');
  const limitEl       = container.querySelector('[data-control="limit"]');
  const spreadEl      = container.querySelector('[data-control="spread"]');
  const maxPerNodeEl  = container.querySelector('[data-control="max_per_node"]');
  const wrap          = container.querySelector("[data-graph-wrap]");
  let canvas          = container.querySelector("[data-graph]");
  let ctx2d           = canvas.getContext("2d");

  let memberFS = filterSelect("Loading…", []);
  container.querySelector('[data-slot="member"]').appendChild(memberFS.el);

  // Load member list
  api("/api/meta/members", {}).then((members) => {
    const opts = members.map((m) => ({ id: m.id, label: m.display_name || m.name }));
    const fs = filterSelect("Type to filter…", opts);
    if (initialParams.member) fs.id = initialParams.member;
    memberFS.el.replaceWith(fs.el);
    memberFS = fs;
  }).catch(() => {});

  layoutEl.value = initialParams.layout || "force";
  timescaleEl.value = initialParams.timescale || "";

  let nodes = [];
  let edges = [];
  let sim   = null;
  let currentLayout = "force";
  let hovered = null;
  let dragged = null;
  let panX = 0, panY = 0, scale = 1;
  let dragStartX, dragStartY;
  let isPanning = false;
  let focusId = null;
  let secondLevelIds = new Set();
  let spreadMult = parseFloat(spreadEl.value) || 1.0;

  function resize() {
    const rect = wrap.getBoundingClientRect();
    canvas.width  = rect.width  * devicePixelRatio;
    canvas.height = rect.height * devicePixelRatio;
    canvas.style.width  = rect.width  + "px";
    canvas.style.height = rect.height + "px";
    ctx2d.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  }

  function toCanvas(ex, ey) {
    const rect = canvas.getBoundingClientRect();
    return [(ex - rect.left - panX) / scale, (ey - rect.top - panY) / scale];
  }

  // ── Layout positioning ────────────────────────────────────────────────

  function findCenterIdx() {
    if (focusId) {
      const i = nodes.findIndex((n) => n.id === focusId);
      if (i >= 0) return i;
    }
    let best = 0, bestT = -1;
    for (let i = 0; i < nodes.length; i++) {
      const t = nodes[i].total_outbound + nodes[i].total_inbound;
      if (t > bestT) { bestT = t; best = i; }
    }
    return best;
  }

  function bfsLayers(startIdx) {
    const adj = {};
    for (const e of edges) {
      (adj[e.source] = adj[e.source] || []).push(e.target);
      (adj[e.target] = adj[e.target] || []).push(e.source);
    }
    const depth = new Map();
    depth.set(startIdx, 0);
    const queue = [startIdx];
    let qi = 0;
    while (qi < queue.length) {
      const cur = queue[qi++];
      for (const nb of (adj[cur] || [])) {
        if (!depth.has(nb)) { depth.set(nb, depth.get(cur) + 1); queue.push(nb); }
      }
    }
    // Assign disconnected nodes to max+1
    const maxD = Math.max(...depth.values(), 0);
    for (let i = 0; i < nodes.length; i++) {
      if (!depth.has(i)) depth.set(i, maxD + 1);
    }
    // Group by layer
    const layers = {};
    for (const [idx, d] of depth) (layers[d] = layers[d] || []).push(idx);
    return layers;
  }

  function zeroVelocities() {
    for (const n of nodes) { n.vx = 0; n.vy = 0; }
  }

  function positionRadial() {
    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    const cx = W / 2, cy = H / 2;
    const layers = bfsLayers(findCenterIdx());
    const maxLayer = Math.max(...Object.keys(layers).map(Number), 1);
    const maxR = Math.min(W, H) * 0.40 * spreadMult;

    for (const [l, indices] of Object.entries(layers)) {
      const d = parseInt(l);
      if (d === 0) {
        nodes[indices[0]].x = cx;
        nodes[indices[0]].y = cy;
      } else {
        const r = (d / maxLayer) * maxR;
        indices.forEach((idx, i) => {
          const angle = (i / indices.length) * Math.PI * 2 - Math.PI / 2;
          nodes[idx].x = cx + Math.cos(angle) * r;
          nodes[idx].y = cy + Math.sin(angle) * r;
        });
      }
    }
    zeroVelocities();
  }

  function positionCircular() {
    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    const cx = W / 2, cy = H / 2;
    const radius = Math.min(W, H) * 0.38 * spreadMult;

    // Greedy ordering: place connected nodes adjacent on the circle
    const adjW = {};
    for (const e of edges) {
      adjW[e.source] = adjW[e.source] || {};
      adjW[e.target] = adjW[e.target] || {};
      adjW[e.source][e.target] = (adjW[e.source][e.target] || 0) + e.weight;
      adjW[e.target][e.source] = (adjW[e.target][e.source] || 0) + e.weight;
    }
    const used = new Set();
    const order = [];
    let cur = findCenterIdx();
    while (order.length < nodes.length) {
      order.push(cur);
      used.add(cur);
      let best = -1, bestW = -1;
      for (const [nStr, w] of Object.entries(adjW[cur] || {})) {
        const n = parseInt(nStr);
        if (!used.has(n) && w > bestW) { best = n; bestW = w; }
      }
      if (best < 0) {
        for (let i = 0; i < nodes.length; i++) { if (!used.has(i)) { best = i; break; } }
      }
      cur = best;
    }

    order.forEach((idx, i) => {
      const angle = (i / order.length) * Math.PI * 2 - Math.PI / 2;
      nodes[idx].x = cx + Math.cos(angle) * radius;
      nodes[idx].y = cy + Math.sin(angle) * radius;
    });
    zeroVelocities();
  }

  function positionHierarchical() {
    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    const layers = bfsLayers(findCenterIdx());
    const maxLayer = Math.max(...Object.keys(layers).map(Number), 1);
    const padX = 60, padY = 60;
    const rowH = (H - padY * 2) / Math.max(maxLayer, 1);

    for (const [l, indices] of Object.entries(layers)) {
      const d = parseInt(l);
      const y = padY + d * rowH;
      const gap = (W - padX * 2) / Math.max(indices.length, 1);
      indices.forEach((idx, i) => {
        nodes[idx].x = padX + gap * (i + 0.5);
        nodes[idx].y = y;
      });
    }
    zeroVelocities();
  }

  // ── Community detection (weighted label propagation) ──────────────────

  let communityOf = {};   // node index → community id
  let commCentres = {};   // community id → {x, y}

  function detectCommunities() {
    // Build weighted adjacency by node index
    const adj = {};
    for (let i = 0; i < nodes.length; i++) adj[i] = [];
    for (const e of edges) {
      adj[e.source].push({ nb: e.target, w: e.weight });
      adj[e.target].push({ nb: e.source, w: e.weight });
    }
    // Each node starts in its own community
    const label = {};
    for (let i = 0; i < nodes.length; i++) label[i] = i;

    const order = Array.from({ length: nodes.length }, (_, i) => i);
    for (let iter = 0; iter < 50; iter++) {
      // Shuffle
      for (let i = order.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [order[i], order[j]] = [order[j], order[i]];
      }
      let changed = false;
      for (const nid of order) {
        if (!adj[nid].length) continue;
        const votes = {};
        for (const { nb, w } of adj[nid]) {
          votes[label[nb]] = (votes[label[nb]] || 0) + w;
        }
        let bestLabel = label[nid], bestW = -1;
        for (const [lbl, w] of Object.entries(votes)) {
          if (w > bestW) { bestW = w; bestLabel = parseInt(lbl); }
        }
        if (bestLabel !== label[nid]) { label[nid] = bestLabel; changed = true; }
      }
      if (!changed) break;
    }
    // Renumber 0, 1, 2, ...
    const unique = [...new Set(Object.values(label))].sort((a, b) => a - b);
    const remap = {};
    unique.forEach((v, i) => remap[v] = i);
    communityOf = {};
    for (let i = 0; i < nodes.length; i++) communityOf[i] = remap[label[i]];
  }

  function miniForceLayout(indices, subEdges, cx, cy, radius) {
    // Small Fruchterman-Reingold for a community cluster
    const n = indices.length;
    if (n === 1) { nodes[indices[0]].x = cx; nodes[indices[0]].y = cy; return; }

    const k = Math.sqrt((radius * radius) / n) * 0.8;
    const idxSet = new Set(indices);
    const pos = {};
    indices.forEach((idx, i) => {
      const angle = (i / n) * Math.PI * 2;
      pos[idx] = { x: Math.cos(angle) * radius * 0.3 + (Math.random() - 0.5) * k, y: Math.sin(angle) * radius * 0.3 + (Math.random() - 0.5) * k };
    });

    let temp = radius * 0.15;
    for (let iter = 0; iter < 120; iter++) {
      const disp = {};
      for (const i of indices) disp[i] = { x: 0, y: 0 };

      // Repulsion
      for (let a = 0; a < indices.length; a++) {
        for (let b = a + 1; b < indices.length; b++) {
          const ia = indices[a], ib = indices[b];
          const dx = pos[ia].x - pos[ib].x, dy = pos[ia].y - pos[ib].y;
          const dist = Math.sqrt(dx * dx + dy * dy) + 0.01;
          const force = (k * k) / dist;
          const fx = (dx / dist) * force, fy = (dy / dist) * force;
          disp[ia].x += fx; disp[ia].y += fy;
          disp[ib].x -= fx; disp[ib].y -= fy;
        }
      }
      // Attraction along edges
      for (const e of subEdges) {
        const dx = pos[e.target].x - pos[e.source].x;
        const dy = pos[e.target].y - pos[e.source].y;
        const dist = Math.sqrt(dx * dx + dy * dy) + 0.01;
        const force = (dist * dist) / k;
        const fx = (dx / dist) * force, fy = (dy / dist) * force;
        disp[e.source].x += fx; disp[e.source].y += fy;
        disp[e.target].x -= fx; disp[e.target].y -= fy;
      }
      // Apply with temperature
      for (const i of indices) {
        const d = Math.sqrt(disp[i].x ** 2 + disp[i].y ** 2) + 0.01;
        const cap = Math.min(d, temp) / d;
        pos[i].x += disp[i].x * cap;
        pos[i].y += disp[i].y * cap;
      }
      temp *= 0.95;
    }
    // Normalize into the cluster radius and offset to centre
    let maxR = 0;
    const mcx = indices.reduce((s, i) => s + pos[i].x, 0) / n;
    const mcy = indices.reduce((s, i) => s + pos[i].y, 0) / n;
    for (const i of indices) {
      const r = Math.sqrt((pos[i].x - mcx) ** 2 + (pos[i].y - mcy) ** 2);
      if (r > maxR) maxR = r;
    }
    const sc = maxR > 0 ? radius / maxR : 1;
    for (const i of indices) {
      nodes[i].x = cx + (pos[i].x - mcx) * sc;
      nodes[i].y = cy + (pos[i].y - mcy) * sc;
    }
  }

  function positionCommunity() {
    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    const cx = W / 2, cy = H / 2;

    detectCommunities();

    // Group node indices by community
    const groups = {};
    for (let i = 0; i < nodes.length; i++) {
      const c = communityOf[i];
      (groups[c] = groups[c] || []).push(i);
    }
    const sorted = Object.keys(groups).map(Number).sort((a, b) => groups[b].length - groups[a].length);
    const nComms = sorted.length;

    // Place community centres on a circle
    commCentres = {};
    if (nComms === 1) {
      commCentres[sorted[0]] = { x: cx, y: cy };
    } else if (nComms === 2) {
      const off = Math.min(W, H) * 0.30 * spreadMult;
      commCentres[sorted[0]] = { x: cx - off, y: cy };
      commCentres[sorted[1]] = { x: cx + off, y: cy };
    } else {
      const r = Math.min(W, H) * 0.32 * spreadMult;
      sorted.forEach((c, i) => {
        const angle = (i / nComms) * Math.PI * 2 - Math.PI / 2;
        commCentres[c] = { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
      });
    }

    // Per-community radius based on relative size — initial positions
    const total = nodes.length;
    for (const c of sorted) {
      const frac = groups[c].length / total;
      const commR = Math.max(40, Math.min(W, H) * 0.28 * Math.sqrt(frac) * spreadMult);
      const idxSet = new Set(groups[c]);
      const subEdges = edges.filter((e) => idxSet.has(e.source) && idxSet.has(e.target));
      miniForceLayout(groups[c], subEdges, commCentres[c].x, commCentres[c].y, commR);
    }
  }

  // ── Physics ───────────────────────────────────────────────────────────
  const BASE_REPULSION = 8000;
  const SPRING_K  = 0.005;
  const BASE_SPRING_LEN = 120;
  const DAMPING   = 0.85;
  const GRAVITY   = 0.02;

  const COMM_GRAVITY = 0.08;  // pull toward community centre

  function tick() {
    if (currentLayout !== "force" && currentLayout !== "community") return;
    const isCommunity = currentLayout === "community";
    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    const cxC = W / 2, cyC = H / 2;
    const REPULSION = BASE_REPULSION * spreadMult * spreadMult;
    const SPRING_LEN = BASE_SPRING_LEN * spreadMult;

    for (let i = 0; i < nodes.length; i++) {
      const ni = nodes[i];
      if (ni === dragged) continue;
      let fx = 0, fy = 0;

      // Repulsion — in community mode, only repel within same community
      for (let j = 0; j < nodes.length; j++) {
        if (i === j) continue;
        if (isCommunity && communityOf[i] !== communityOf[j]) continue;
        const nj = nodes[j];
        const dx = ni.x - nj.x, dy = ni.y - nj.y;
        const dist2 = dx * dx + dy * dy + 1;
        const dist = Math.sqrt(dist2);
        const force = REPULSION / dist2;
        fx += (dx / dist) * force;
        fy += (dy / dist) * force;
      }

      // Spring attraction along edges
      for (const e of edges) {
        let other = -1;
        if (e.source === i) other = e.target;
        else if (e.target === i) other = e.source;
        if (other < 0) continue;
        const nj = nodes[other];
        const dx = nj.x - ni.x, dy = nj.y - ni.y;
        const dist = Math.sqrt(dx * dx + dy * dy) + 1;
        const displacement = dist - SPRING_LEN;
        // Weaker cross-community springs so clusters don't merge
        const crossScale = (isCommunity && communityOf[i] !== communityOf[other]) ? 0.15 : 1;
        const force = SPRING_K * displacement * (1 + e.weight * 0.01) * crossScale;
        fx += (dx / dist) * force;
        fy += (dy / dist) * force;
      }

      // Gravity: global center for force, community centre for community
      if (isCommunity) {
        const cc = commCentres[communityOf[i]];
        if (cc) {
          fx += (cc.x - ni.x) * COMM_GRAVITY;
          fy += (cc.y - ni.y) * COMM_GRAVITY;
        }
      } else {
        fx += (cxC - ni.x) * GRAVITY;
        fy += (cyC - ni.y) * GRAVITY;
      }

      ni.vx = (ni.vx + fx) * DAMPING;
      ni.vy = (ni.vy + fy) * DAMPING;
    }

    for (const n of nodes) {
      if (n === dragged) continue;
      n.x += n.vx;
      n.y += n.vy;
    }
  }

  function draw() {
    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    ctx2d.clearRect(0, 0, W, H);
    ctx2d.save();
    ctx2d.translate(panX, panY);
    ctx2d.scale(scale, scale);

    // Edges
    const maxWeight = edges.reduce((m, e) => Math.max(m, e.weight), 1);
    const useCurves = currentLayout === "circular";
    const cxC = W / 2, cyC = H / 2;
    for (const e of edges) {
      const a = nodes[e.source], b = nodes[e.target];
      const hovEdge = hovered && (e.source === hovered._idx || e.target === hovered._idx);
      ctx2d.strokeStyle = hovEdge ? "rgba(235,69,158,0.6)" : EDGE_CLR;
      ctx2d.lineWidth = Math.max(0.5, (e.weight / maxWeight) * 5);
      ctx2d.beginPath();
      ctx2d.moveTo(a.x, a.y);
      if (useCurves) {
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        const pull = 0.5;
        ctx2d.quadraticCurveTo(mx + (cxC - mx) * pull, my + (cyC - my) * pull, b.x, b.y);
      } else {
        ctx2d.lineTo(b.x, b.y);
      }
      ctx2d.stroke();
    }

    // Nodes
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const isHov = hovered && hovered._idx === i;
      let color = NODE_CLR;
      if (currentLayout === "community" && communityOf[i] !== undefined) {
        color = COMMUNITY_COLORS[communityOf[i] % COMMUNITY_COLORS.length];
      } else if (focusId && n.id === focusId) {
        color = FOCUS_CLR;
      } else if (secondLevelIds.has(n.id)) {
        color = NODE_2ND;
      }
      if (isHov) color = HIGHLIGHT;

      ctx2d.beginPath();
      ctx2d.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx2d.fillStyle = color;
      ctx2d.fill();

      const fontSize = Math.max(9, Math.min(12, n.r * 0.9));
      ctx2d.font = `${fontSize}px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif`;
      ctx2d.fillStyle = TEXT_CLR;
      ctx2d.textAlign = "center";
      ctx2d.fillText(n.name, n.x, n.y - n.r - 4);
    }

    // Tooltip
    if (hovered) {
      const n = hovered;
      const connEdges = edges.filter((e) => e.source === n._idx || e.target === n._idx);
      const lines = [
        n.name,
        `Out: ${n.total_outbound}  In: ${n.total_inbound}`,
        `Partners: ${n.unique_partners}  Edges shown: ${connEdges.length}`,
      ];
      if (currentLayout === "community" && communityOf[n._idx] !== undefined) {
        lines.push(`Community: ${communityOf[n._idx] + 1}`);
      }
      const pad = 6;
      ctx2d.font = "11px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      const boxW = Math.max(...lines.map((l) => ctx2d.measureText(l).width)) + pad * 2;
      const lineH = 15, boxH = lines.length * lineH + pad * 2;
      const bx = n.x + n.r + 8, by = n.y - boxH / 2;
      ctx2d.fillStyle = "rgba(24,25,28,0.92)"; ctx2d.strokeStyle = "#3f4147"; ctx2d.lineWidth = 1;
      ctx2d.beginPath(); ctx2d.roundRect(bx, by, boxW, boxH, 4); ctx2d.fill(); ctx2d.stroke();
      ctx2d.fillStyle = TEXT_CLR; ctx2d.textAlign = "left";
      lines.forEach((l, i) => ctx2d.fillText(l, bx + pad, by + pad + (i + 1) * lineH - 3));
    }

    ctx2d.restore();
  }

  function animate() { tick(); draw(); sim = requestAnimationFrame(animate); }

  // ── Mouse interaction ─────────────────────────────────────────────────

  function hitTest(ex, ey) {
    const [cx, cy] = toCanvas(ex, ey);
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      const dx = cx - n.x, dy = cy - n.y;
      if (dx * dx + dy * dy <= (n.r + 4) * (n.r + 4)) { n._idx = i; return n; }
    }
    return null;
  }

  canvas.addEventListener("mousedown", (e) => {
    const hit = hitTest(e.clientX, e.clientY);
    if (hit) { dragged = hit; wrap.style.cursor = "grabbing"; }
    else { isPanning = true; dragStartX = e.clientX - panX; dragStartY = e.clientY - panY; wrap.style.cursor = "grabbing"; }
  });
  canvas.addEventListener("mousemove", (e) => {
    if (dragged) { const [cx, cy] = toCanvas(e.clientX, e.clientY); dragged.x = cx; dragged.y = cy; dragged.vx = 0; dragged.vy = 0; }
    else if (isPanning) { panX = e.clientX - dragStartX; panY = e.clientY - dragStartY; }
    else { hovered = hitTest(e.clientX, e.clientY); wrap.style.cursor = hovered ? "pointer" : "grab"; }
  });
  canvas.addEventListener("mouseup", () => { dragged = null; isPanning = false; wrap.style.cursor = "grab"; });
  canvas.addEventListener("mouseleave", () => { dragged = null; isPanning = false; hovered = null; wrap.style.cursor = "grab"; });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    const newScale = Math.max(0.2, Math.min(5, scale * delta));
    panX = mx - (mx - panX) * (newScale / scale);
    panY = my - (my - panY) * (newScale / scale);
    scale = newScale;
  }, { passive: false });

  // ── Client-side filtering (mirrors /connection_web logic) ─────────────

  function applyFilters(data) {
    const minPct = (parseInt(minPctEl.value) || 0) / 100;
    const maxPerNode = parseInt(maxPerNodeEl.value) || 0;
    const layerCount = parseInt(layersEl.value) || 2;
    focusId = memberFS.id || null;
    secondLevelIds = new Set();

    // Build node total interaction map
    const nodeTotal = {};
    for (const p of data.top_pairs) {
      nodeTotal[p.from_id] = (nodeTotal[p.from_id] || 0) + p.weight;
      nodeTotal[p.to_id]   = (nodeTotal[p.to_id]   || 0) + p.weight;
    }

    function pctPasses(fromId, toId, w) {
      if (minPct <= 0) return true;
      const denom = Math.min(nodeTotal[fromId] || 1, nodeTotal[toId] || 1);
      return w >= minPct * denom;
    }

    let filteredPairs = data.top_pairs;

    // Focus member: expand layers
    if (focusId) {
      const included = new Set([focusId]);
      let frontier = new Set([focusId]);

      for (let layer = 0; layer < layerCount; layer++) {
        const newNodes = new Set();
        for (const p of filteredPairs) {
          if (!pctPasses(p.from_id, p.to_id, p.weight)) continue;
          if (frontier.has(p.from_id) && !included.has(p.to_id)) newNodes.add(p.to_id);
          if (frontier.has(p.to_id) && !included.has(p.from_id)) newNodes.add(p.from_id);
        }
        if (!newNodes.size) break;
        if (layer > 0) newNodes.forEach((id) => secondLevelIds.add(id));
        newNodes.forEach((id) => included.add(id));
        frontier = newNodes;
      }

      filteredPairs = filteredPairs.filter(
        (p) => included.has(p.from_id) && included.has(p.to_id) && pctPasses(p.from_id, p.to_id, p.weight)
      );
    } else {
      filteredPairs = filteredPairs.filter((p) => pctPasses(p.from_id, p.to_id, p.weight));
    }

    // Max edges per node
    if (maxPerNode > 0) {
      const adj = {};
      for (const p of filteredPairs) {
        (adj[p.from_id] = adj[p.from_id] || []).push(p);
        (adj[p.to_id]   = adj[p.to_id]   || []).push(p);
      }
      const nodeTop = {};
      for (const [nid, elist] of Object.entries(adj)) {
        elist.sort((a, b) => b.weight - a.weight);
        nodeTop[nid] = new Set(elist.slice(0, maxPerNode).map(
          (p) => p.from_id === nid ? p.to_id : p.from_id
        ));
      }
      filteredPairs = filteredPairs.filter(
        (p) => (nodeTop[p.from_id] || new Set()).has(p.to_id) && (nodeTop[p.to_id] || new Set()).has(p.from_id)
      );
    }

    // Collect nodes from remaining edges
    const nodeIds = new Set();
    for (const p of filteredPairs) { nodeIds.add(p.from_id); nodeIds.add(p.to_id); }
    const filteredNodes = data.nodes.filter((n) => nodeIds.has(n.user_id));

    return { nodes: filteredNodes, pairs: filteredPairs };
  }

  // ── Data loading ──────────────────────────────────────────────────────

  let cachedData = null;

  async function fetchData() {
    const params = { limit: parseInt(limitEl.value) || 40 };
    const d = parseInt(timescaleEl.value);
    if (!isNaN(d) && d > 0) params.days = d;
    cachedData = await api("/api/reports/interaction-graph", params);
    rebuildGraph();
  }

  function rebuildGraph() {
    if (!cachedData) return;

    const qs = new URLSearchParams();
    qs.set("layout", layoutEl.value);
    if (timescaleEl.value) qs.set("timescale", timescaleEl.value);
    if (memberFS.id) qs.set("member", memberFS.id);
    qs.set("min_pct", minPctEl.value);
    qs.set("layers", layersEl.value);
    qs.set("limit", limitEl.value);
    qs.set("spread", spreadEl.value);
    qs.set("max_per_node", maxPerNodeEl.value);
    history.replaceState(null, "", `#/connection-graph?${qs}`);

    if (sim) { cancelAnimationFrame(sim); sim = null; }
    spreadMult = parseFloat(spreadEl.value) || 1.0;

    const { nodes: fNodes, pairs } = applyFilters(cachedData);

    if (!fNodes.length) {
      wrap.innerHTML = `<div class="empty" style="padding:40px; text-align:center;">No connections meet the current filters.</div>`;
      return;
    }

    // Ensure canvas exists
    if (!wrap.querySelector("[data-graph]")) {
      wrap.innerHTML = `<canvas data-graph></canvas>`;
      canvas = wrap.querySelector("[data-graph]");
      ctx2d = canvas.getContext("2d");
      // Rebind mouse events
    }

    resize();

    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    panX = 0; panY = 0; scale = 1;

    // Build nodes
    nodes = [];
    edges = [];
    const nodeMap = new Map();
    const maxTotal = fNodes.reduce((m, n) => Math.max(m, n.total_outbound + n.total_inbound), 1);

    fNodes.forEach((n, i) => {
      const total = n.total_outbound + n.total_inbound;
      const r = 6 + (total / maxTotal) * 22;
      const angle = (i / fNodes.length) * Math.PI * 2;
      const sp = Math.min(W, H) * 0.30 * spreadMult;
      nodeMap.set(n.user_id, i);
      nodes.push({
        id: n.user_id,
        name: n.user_name || n.user_id,
        x: W / 2 + Math.cos(angle) * sp + (Math.random() - 0.5) * 30,
        y: H / 2 + Math.sin(angle) * sp + (Math.random() - 0.5) * 30,
        vx: 0, vy: 0, r,
        total_outbound: n.total_outbound,
        total_inbound: n.total_inbound,
        unique_partners: n.unique_partners,
      });
    });

    for (const p of pairs) {
      const si = nodeMap.get(p.from_id), ti = nodeMap.get(p.to_id);
      if (si !== undefined && ti !== undefined) {
        edges.push({ source: si, target: ti, weight: p.weight });
      }
    }

    currentLayout = layoutEl.value;
    if (currentLayout === "community") positionCommunity();
    else if (currentLayout === "radial") positionRadial();
    else if (currentLayout === "circular") positionCircular();
    else if (currentLayout === "hierarchical") positionHierarchical();

    animate();
  }

  // Controls: fetch when data source changes, rebuild when filters change
  timescaleEl.addEventListener("change", fetchData);
  limitEl.addEventListener("change", fetchData);
  layoutEl.addEventListener("change", rebuildGraph);
  for (const el of [minPctEl, layersEl, maxPerNodeEl]) el.addEventListener("change", rebuildGraph);
  spreadEl.addEventListener("input", rebuildGraph);
  // Watch for member selection — rebuild after dropdown closes
  const memberSlot = container.querySelector('[data-slot="member"]');
  let lastMemberId = memberFS.id;
  memberSlot.addEventListener("focusout", () => {
    setTimeout(() => {
      if (memberFS.id !== lastMemberId) { lastMemberId = memberFS.id; rebuildGraph(); }
    }, 200);
  });

  resize();
  fetchData();

  const ro = new ResizeObserver(() => resize());
  ro.observe(wrap);

  return {
    unmount() {
      if (sim) cancelAnimationFrame(sim);
      ro.disconnect();
    },
  };
}
