import { api } from "../api.js";
import { filterSelect } from "../filter-select.js";
import { renderEmpty, renderError, renderLoading } from "../states.js";
import { rangePicker } from "../report-helpers.js";
import {
  ROLE_COLORS,
  GRAPH_CLUSTERS, GRAPH_EDGE, SERIES_OVERFLOW,
  CHART_SURFACE, CHART_TEXT, CHART_GRID, CHART_ACCENT,
} from "../charts.js";

// Force-directed network graph rendered on a <canvas>. Restored 2026-08-26
// after being removed in 5b4cd71d ("same endpoint as Interactions") — true of
// the endpoint, but Interactions renders it as two tables and a bar chart.
// Redesigned 2026-08-29 into a single full-height stage: the canvas takes all
// remaining viewport, controls collapse into a chip bar (with the numeric
// tuning knobs behind one popover), and the community chips overlaid on the
// canvas are both the legend and the cluster filter. The scorecard, bridge
// and cluster tables, cross-cluster heatmap and isolates list were dropped
// with it — one big connection web, nothing competing. The endpoint still
// serves all of that; only this surface changed.
//
// Colour discipline: everything comes from the ONE shared palette in
// charts.js — cluster fills from GRAPH_CLUSTERS (the documented network-graph
// extension of ROLE_COLORS; see charts.js for the validation), edges from
// GRAPH_EDGE at weight-scaled alpha, focus/hover accents from CHART_ACCENT.
// The dashboard is dark-only, and the canvas keeps the same dark surface as
// every other chart.
const BG        = CHART_SURFACE;
const NODE_2ND  = ROLE_COLORS[1];
const TEXT_CLR  = CHART_TEXT;
const HIGHLIGHT = CHART_ACCENT;

// Edges fade with weight so strong ties read and weak ones recede — a flat
// alpha was the "greyed over" regression this replaced. Range chosen so the
// weakest edge is still findable and the strongest never occludes a node.
const EDGE_ALPHA_MIN = 0.14;
const EDGE_ALPHA_MAX = 0.55;

// The most prominent nodes keep standing labels; everything else labels on
// hover (the node and its neighbours). Labelling all 40 every frame was the
// other half of the grey haze.
const LABELLED_NODES = 10;

/** `#rrggbb` at the given alpha — canvas takes no CSS colour functions. */
function _alpha(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

// Cluster colour is a grouping cue, not a legend-matched series: communities
// sit apart on the canvas, so GRAPH_CLUSTERS' eight validated slots cover
// every community the server detects on both live servers. Past eight the
// tail still folds to the overflow neutral rather than inventing hues.
function clusterColor(id) {
  return GRAPH_CLUSTERS[Number(id) || 0] || SERIES_OVERFLOW;
}

export function mount(container, initialParams) {
  // initialParams comes straight off the location hash, and the numeric ones
  // are interpolated into innerHTML below — coerce them so a crafted link
  // can't put markup into a value attribute.
  const num = (v, dflt) => {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n : dflt;
  };
  const p0 = {
    min_pct:      num(initialParams.min_pct, 5),
    layers:       num(initialParams.layers, 2),
    limit:        num(initialParams.limit, 40),
    max_per_node: num(initialParams.max_per_node, 3),
    spread:       num(initialParams.spread, 1.0),
    resolution:   num(initialParams.resolution, 1.2),
  };
  container.innerHTML = `
    <div class="panel graph-stage">
      <header>
        <h2>Connection Graph</h2>
        <div class="subtitle">Visual network of who interacts with whom</div>
      </header>

      <div class="graph-chipbar">
        <select data-control="layout" aria-label="Layout">
          <option value="force">Force-directed</option>
          <option value="community">Community clusters</option>
          <option value="radial">Radial</option>
          <option value="circular">Circular</option>
          <option value="hierarchical">Hierarchical</option>
        </select>
        <span data-slot="period"></span>
        <span data-slot="member"></span>
        <details class="graph-tuning" data-tuning>
          <summary>Tuning</summary>
          <div class="graph-tuning-pop">
            <label>Min edge %
              <input type="number" data-control="min_pct" min="0" max="100" value="${p0.min_pct}" title="Hide connections weaker than this share of a member's interactions" />
            </label>
            <label>Layers
              <input type="number" data-control="layers" min="1" max="5" value="${p0.layers}" title="How many hops out from the focused member to include" />
            </label>
            <label>Max nodes
              <input type="number" data-control="limit" min="5" max="100" value="${p0.limit}" />
            </label>
            <label>Max edges per node
              <input type="number" data-control="max_per_node" min="0" max="20" value="${p0.max_per_node}" title="Most connections to draw per person (0 = no limit)" />
            </label>
            <label>Spread
              <input type="range" data-control="spread" min="0.5" max="3" step="0.1" value="${p0.spread}" />
            </label>
            <label title="Higher values break large clusters apart; lower values merge them. Applied server-side.">Granularity
              <span class="graph-tuning-range">
                <input type="range" data-control="resolution" min="0.5" max="3.0" step="0.1" value="${p0.resolution}" />
                <span data-resolution-val>${p0.resolution}</span>
              </span>
            </label>
          </div>
        </details>
        <button type="button" data-replay class="graph-chip" title="Replay the network week by week — watch people arrive, leave, and drift between groups">Replay</button>
      </div>

      <div data-graph-wrap style="position:relative; flex:1; min-height:0; background:${BG}; border-radius:8px; overflow:hidden; cursor:grab;">
        <canvas data-graph></canvas>
        <div data-graph-msg class="graph-msg" hidden></div>
        <button data-fullscreen class="btn btn-icon" title="Toggle fullscreen" style="position:absolute;top:6px;right:6px;z-index:5;background:color-mix(in srgb, var(--bg-floor) 60%, transparent);">⛶</button>
        <div data-cluster-chips class="graph-clusterbar" role="group" aria-label="Communities — click a chip to hide or show that group"></div>
        <div class="graph-hint">drag · ctrl+scroll zooms · size = interactions · colour = friend group</div>
        <div data-rp-notice class="graph-notice" hidden></div>
        <div data-replaybar class="graph-replaybar" hidden>
          <button type="button" data-rp-toggle title="Play / pause">▶</button>
          <input type="range" data-rp-scrub min="0" max="0" step="1" value="0" aria-label="Replay position" />
          <span data-rp-date></span>
          <select data-rp-speed aria-label="Replay speed">
            <option value="1">1×</option>
            <option value="2" selected>2×</option>
            <option value="4">4×</option>
          </select>
          <button type="button" data-rp-close title="Exit replay">✕</button>
        </div>
      </div>
    </div>
  `;

  const layoutEl      = container.querySelector('[data-control="layout"]');
  // Standard day-range picker (report-helpers) in place of the old bespoke
  // "Period" select, so every report offers the same choices.
  const periodPicker  = rangePicker({
    value: initialParams.timescale || "",
    allowAll: true,
    label: "Period",
  });
  const timescaleEl   = periodPicker.querySelector("select");
  timescaleEl.dataset.control = "timescale";
  container.querySelector('[data-slot="period"]').replaceWith(periodPicker);
  const minPctEl      = container.querySelector('[data-control="min_pct"]');
  const layersEl      = container.querySelector('[data-control="layers"]');
  const limitEl       = container.querySelector('[data-control="limit"]');
  const spreadEl      = container.querySelector('[data-control="spread"]');
  const resolutionEl  = container.querySelector('[data-control="resolution"]');
  const resolutionValEl = container.querySelector('[data-resolution-val]');
  const maxPerNodeEl  = container.querySelector('[data-control="max_per_node"]');
  const fullscreenBtn = container.querySelector('[data-fullscreen]');
  const clusterChipsEl = container.querySelector("[data-cluster-chips]");
  const replayBtn     = container.querySelector("[data-replay]");
  const replayBar     = container.querySelector("[data-replaybar]");
  const rpToggle      = container.querySelector("[data-rp-toggle]");
  const rpScrub       = container.querySelector("[data-rp-scrub]");
  const rpDate        = container.querySelector("[data-rp-date]");
  const rpSpeed       = container.querySelector("[data-rp-speed]");
  const rpClose       = container.querySelector("[data-rp-close]");
  const rpNotice      = container.querySelector("[data-rp-notice]");
  const wrap          = container.querySelector("[data-graph-wrap]");
  // The canvas is created once and never replaced — loading/empty/error states
  // render as an overlay on top of it, so the mouse listeners bound below stay
  // live through every empty → data cycle.
  const canvas        = container.querySelector("[data-graph]");
  const ctx2d         = canvas.getContext("2d");
  const msgEl         = container.querySelector("[data-graph-msg]");

  function showMessage(html) {
    msgEl.innerHTML = html;
    msgEl.hidden = false;
  }
  function clearMessage() {
    msgEl.innerHTML = "";
    msgEl.hidden = true;
  }

  // Clusters hidden by the user (cluster_id values). Empty = show all.
  const hiddenClusters = new Set();
  // Cluster-id lookup keyed by user_id string, populated from API metrics.
  let clusterByUser = {};

  const memberFS = filterSelect("Type to filter…", [], {
    label: "Focus member",
    emptyLabel: "(no focus member)",
  });
  container.querySelector('[data-slot="member"]').appendChild(memberFS.el);

  // Load member list
  api("/api/meta/members", {}).then((members) => {
    const opts = members.map((m) => ({
      id: m.id,
      label: (m.display_name || m.name) + (m.left_server ? " (left)" : ""),
      left: !!m.left_server,
    })).sort((a, b) => a.left - b.left || a.label.localeCompare(b.label));
    memberFS.setOptions(opts);
    if (initialParams.member) {
      memberFS.setValue(initialParams.member);
      lastMemberId = memberFS.getValue();
      rebuildGraph();
    }
  }).catch(() => {});

  layoutEl.value = initialParams.layout || "community";
  timescaleEl.value = initialParams.timescale || "";
  if (initialParams.hidden_clusters) {
    for (const cid of String(initialParams.hidden_clusters).split(",")) {
      const n = parseInt(cid);
      if (!isNaN(n)) hiddenClusters.add(n);
    }
  }

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
  // Node indices that keep a standing label — the LABELLED_NODES most active,
  // recomputed whenever the node set changes.
  let labelledIdx = new Set();

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
  let commCenters = {};   // community id → {x, y}

  function detectCommunities() {
    // The server's clustering assignment (cluster_id, tunable via the
    // Granularity knob) is the ONE partition this panel speaks: the chips,
    // the node fills and this layout's grouping all read it. The client-side
    // label propagation this replaced computed its own partition, so the
    // default layout could disagree with the chip legend sitting next to it.
    communityOf = {};
    for (let i = 0; i < nodes.length; i++) communityOf[i] = nodes[i].cluster_id ?? 0;
  }

  function miniForceLayout(indices, subEdges, cx, cy, radius) {
    // Small Fruchterman-Reingold for a community cluster
    const n = indices.length;
    if (n === 1) { nodes[indices[0]].x = cx; nodes[indices[0]].y = cy; return; }

    const k = Math.sqrt((radius * radius) / n) * 0.8;
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
    // Normalize into the cluster radius and offset to center
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

    // Place community centers on a circle
    commCenters = {};
    if (nComms === 1) {
      commCenters[sorted[0]] = { x: cx, y: cy };
    } else if (nComms === 2) {
      const off = Math.min(W, H) * 0.30 * spreadMult;
      commCenters[sorted[0]] = { x: cx - off, y: cy };
      commCenters[sorted[1]] = { x: cx + off, y: cy };
    } else {
      const r = Math.min(W, H) * 0.32 * spreadMult;
      sorted.forEach((c, i) => {
        const angle = (i / nComms) * Math.PI * 2 - Math.PI / 2;
        commCenters[c] = { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
      });
    }

    // Per-community radius based on relative size — initial positions
    const total = nodes.length;
    for (const c of sorted) {
      const frac = groups[c].length / total;
      const commR = Math.max(40, Math.min(W, H) * 0.28 * Math.sqrt(frac) * spreadMult);
      const idxSet = new Set(groups[c]);
      const subEdges = edges.filter((e) => idxSet.has(e.source) && idxSet.has(e.target));
      miniForceLayout(groups[c], subEdges, commCenters[c].x, commCenters[c].y, commR);
    }
  }

  // ── Physics ───────────────────────────────────────────────────────────
  const BASE_REPULSION = 8000;
  const SPRING_K  = 0.005;
  const BASE_SPRING_LEN = 120;
  const DAMPING   = 0.85;
  const GRAVITY   = 0.02;

  const COMM_GRAVITY = 0.08;  // pull toward community center

  /** Advance the simulation one step. Returns the fastest node speed so the
   *  animation loop can stop once the layout has settled (W-D10). Static
   *  layouts never move, so they report 0 and the loop ends after one draw. */
  function tick() {
    if (currentLayout !== "force" && currentLayout !== "community") return 0;
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

      // Gravity: global center for force, community center for community
      if (isCommunity) {
        const cc = commCenters[communityOf[i]];
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

    let fastest = 0;
    for (const n of nodes) {
      if (n === dragged) continue;
      n.x += n.vx;
      n.y += n.vy;
      const speed = Math.abs(n.vx) + Math.abs(n.vy);
      if (speed > fastest) fastest = speed;
    }
    return fastest;
  }

  function draw() {
    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    ctx2d.clearRect(0, 0, W, H);
    ctx2d.save();
    ctx2d.translate(panX, panY);
    ctx2d.scale(scale, scale);

    // Hover context: the hovered node's neighbourhood stays at full strength
    // while everything else recedes, so a crowded graph answers "who does
    // this person talk to" without a click.
    const hovIdx = hovered ? hovered._idx : -1;
    const hovNbrs = new Set();
    if (hovIdx >= 0) {
      for (const e of edges) {
        if (e.source === hovIdx) hovNbrs.add(e.target);
        else if (e.target === hovIdx) hovNbrs.add(e.source);
      }
    }

    // Edges — drawn under the nodes, with weight carrying opacity as well as
    // width so strong ties read and weak ones recede.
    const maxWeight = edges.reduce((m, e) => Math.max(m, e.weight), 1);
    const useCurves = currentLayout === "circular";
    const cxC = W / 2, cyC = H / 2;
    for (const e of edges) {
      const a = nodes[e.source], b = nodes[e.target];
      const hovEdge = hovIdx >= 0 && (e.source === hovIdx || e.target === hovIdx);
      const wFrac = e.weight / maxWeight;
      const fade = hovIdx >= 0 && !hovEdge ? 0.35 : 1;
      ctx2d.strokeStyle = hovEdge
        ? _alpha(CHART_ACCENT, 0.85)
        : _alpha(GRAPH_EDGE, (EDGE_ALPHA_MIN + wFrac * (EDGE_ALPHA_MAX - EDGE_ALPHA_MIN)) * fade);
      ctx2d.lineWidth = Math.max(0.6, wFrac * 4);
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
      const isHov = hovIdx === i;
      // Fill is ALWAYS the server cluster — the community chips are the
      // legend, so node colour and chip colour must read the same partition
      // in every layout. The focused member and the hovered node keep their
      // cluster fill and wear an accent RING instead: the accent is a
      // marker, not an identity, and a fill swap would impersonate cluster 5
      // exactly (CHART_ACCENT is GRAPH_CLUSTERS[4]). Outer focus rings keep
      // their moss fill — depth outranks community while focused.
      const isFocus = focusId && n.id === focusId;
      let color = clusterColor(n.cluster_id ?? 0);
      if (!isFocus && secondLevelIds.has(n.id)) color = NODE_2ND;

      const dimmed = hovIdx >= 0 && !isHov && !hovNbrs.has(i);
      ctx2d.globalAlpha = dimmed ? 0.35 : 1;

      ctx2d.beginPath();
      ctx2d.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx2d.fillStyle = color;
      ctx2d.fill();
      // Ring: accent when marked (focus or hover), surface-colour otherwise
      // — the surface ring keeps overlapping nodes countable, and it is the
      // secondary encoding the extended palette's CVD floor band requires.
      if (isFocus || isHov) {
        ctx2d.strokeStyle = HIGHLIGHT;
        ctx2d.lineWidth = 2.5;
      } else {
        ctx2d.strokeStyle = BG;
        ctx2d.lineWidth = 2;
      }
      ctx2d.stroke();

      // Standing labels only for the most prominent nodes (plus the focus
      // member); the rest label on hover, node and neighbours together.
      const labelled = labelledIdx.has(i) || isHov || hovNbrs.has(i) || isFocus;
      if (labelled) {
        const fontSize = Math.max(10, Math.min(13, n.r * 0.9));
        ctx2d.font = `500 ${fontSize}px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif`;
        ctx2d.textAlign = "center";
        // Halo keeps the name legible where it crosses an edge.
        ctx2d.strokeStyle = _alpha(BG, 0.85);
        ctx2d.lineWidth = 3;
        ctx2d.strokeText(n.name, n.x, n.y - n.r - 5);
        ctx2d.fillStyle = TEXT_CLR;
        ctx2d.fillText(n.name, n.x, n.y - n.r - 5);
      }
      ctx2d.globalAlpha = 1;
    }

    // Tooltip
    if (hovered) {
      const n = hovered;
      const connEdges = edges.filter((e) => e.source === n._idx || e.target === n._idx);
      const ratio = n.total_inbound > 0 ? (n.total_outbound / n.total_inbound).toFixed(2) : "∞";
      const lines = replay
        ? [
            n.name,
            `${n.total_outbound} interactions this window`,
            `Partners shown: ${connEdges.length}`,
            `Cluster: ${(n.cluster_id ?? 0) + 1}`,
          ]
        : [
            n.name,
            `Out: ${n.total_outbound}  In: ${n.total_inbound}  (ratio ${ratio})`,
            `Partners: ${n.unique_partners}  Edges shown: ${connEdges.length}`,
            `Cluster: ${(n.cluster_id ?? 0) + 1}`,
          ];
      const pad = 6;
      ctx2d.font = "11px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      const boxW = Math.max(...lines.map((l) => ctx2d.measureText(l).width)) + pad * 2;
      const lineH = 15, boxH = lines.length * lineH + pad * 2;
      const bx = n.x + n.r + 8, by = n.y - boxH / 2;
      ctx2d.fillStyle = "rgba(24,25,28,0.92)"; ctx2d.strokeStyle = CHART_GRID; ctx2d.lineWidth = 1;
      ctx2d.beginPath(); ctx2d.roundRect(bx, by, boxW, boxH, 4); ctx2d.fill(); ctx2d.stroke();
      ctx2d.fillStyle = TEXT_CLR; ctx2d.textAlign = "left";
      lines.forEach((l, i) => ctx2d.fillText(l, bx + pad, by + pad + (i + 1) * lineH - 3));
    }

    ctx2d.restore();
  }

  // Repaint strategy (W-D10): the simulation loop runs only while the layout
  // is actually moving. Static layouts (radial/circular/hierarchical) and a
  // cooled force layout draw on demand instead of burning a frame every 16ms.
  const SETTLED_SPEED = 0.05;
  let drawQueued = false;

  /** One repaint on the next frame, unless the sim loop is already running. */
  function requestDraw() {
    if (sim || drawQueued) return;
    drawQueued = true;
    requestAnimationFrame(() => { drawQueued = false; draw(); });
  }

  function animate() {
    const speed = tick();
    draw();
    if (dragged || isPanning || speed > SETTLED_SPEED) {
      sim = requestAnimationFrame(animate);
    } else {
      sim = null; // settled — stop repainting until something changes
    }
  }

  /** Restart the physics loop (dynamic layouts) or schedule a single repaint. */
  function startSim() {
    if (currentLayout !== "force" && currentLayout !== "community") {
      requestDraw();
      return;
    }
    if (sim) return;
    sim = requestAnimationFrame(animate);
  }

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
    startSim();
  });
  canvas.addEventListener("mousemove", (e) => {
    const wasHovered = hovered;
    if (dragged) { const [cx, cy] = toCanvas(e.clientX, e.clientY); dragged.x = cx; dragged.y = cy; dragged.vx = 0; dragged.vy = 0; }
    else if (isPanning) { panX = e.clientX - dragStartX; panY = e.clientY - dragStartY; }
    else { hovered = hitTest(e.clientX, e.clientY); wrap.style.cursor = hovered ? "pointer" : "grab"; }
    if (dragged || isPanning || hovered !== wasHovered) requestDraw();
  });
  canvas.addEventListener("mouseup", () => { dragged = null; isPanning = false; wrap.style.cursor = "grab"; requestDraw(); });
  canvas.addEventListener("mouseleave", () => { dragged = null; isPanning = false; hovered = null; wrap.style.cursor = "grab"; requestDraw(); });
  canvas.addEventListener("wheel", (e) => {
    // Ctrl+scroll to zoom, matching the chart panels — a bare scroll keeps
    // scrolling the page instead of trapping it over the graph.
    if (!e.ctrlKey) return;
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    const newScale = Math.max(0.2, Math.min(5, scale * delta));
    panX = mx - (mx - panX) * (newScale / scale);
    panY = my - (my - panY) * (newScale / scale);
    scale = newScale;
    requestDraw();
  }, { passive: false });

  // ── Client-side filtering (mirrors /connection_web logic) ─────────────

  function applyFilters(data) {
    const minPct = (parseInt(minPctEl.value) || 0) / 100;
    const maxPerNode = parseInt(maxPerNodeEl.value) || 0;
    const layerCount = parseInt(layersEl.value) || 2;
    focusId = memberFS.getValue() || null;
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

    // Cluster filter — drop edges whose endpoints are in a hidden cluster
    if (hiddenClusters.size) {
      filteredPairs = filteredPairs.filter((p) => {
        const ca = clusterByUser[p.from_id];
        const cb = clusterByUser[p.to_id];
        return !hiddenClusters.has(ca) && !hiddenClusters.has(cb);
      });
    }

    // Collect nodes from remaining edges
    const nodeIds = new Set();
    for (const p of filteredPairs) { nodeIds.add(p.from_id); nodeIds.add(p.to_id); }
    let filteredNodes = data.nodes.filter((n) => nodeIds.has(n.user_id));
    if (hiddenClusters.size) {
      filteredNodes = filteredNodes.filter((n) => !hiddenClusters.has(n.cluster_id));
    }

    return { nodes: filteredNodes, pairs: filteredPairs };
  }

  // ── Community chips ───────────────────────────────────────────────────

  // The coloured chips overlaid on the canvas are the legend AND the cluster
  // filter in one control: each chip carries its community's fill, and
  // clicking it hides or shows that group. Hidden communities keep their
  // chip (hollow, muted) so the way back is always visible.
  let clusterList = [];

  function renderClusterChips(clusters) {
    clusterList = clusters || [];
    if (!clusterList.length) {
      clusterChipsEl.innerHTML = "";
      return;
    }
    clusterChipsEl.innerHTML = clusterList.map((c, i) => {
      const color = clusterColor(c.id);
      const hidden = hiddenClusters.has(c.id);
      return `<button type="button" class="graph-cluster-chip${hidden ? " is-hidden" : ""}"
        data-cluster-toggle="${c.id}"
        title="Cluster ${i + 1} — ${c.size} member${c.size === 1 ? "" : "s"}. Click to ${hidden ? "show" : "hide"}."
        aria-pressed="${!hidden}">
        <span class="dot" style="${hidden ? `border-color:${color};` : `background:${color};`}"></span>${i + 1}
      </button>`;
    }).join("");
    clusterChipsEl.querySelectorAll("[data-cluster-toggle]").forEach((chip) => {
      chip.addEventListener("click", () => {
        const cid = parseInt(chip.dataset.clusterToggle);
        if (hiddenClusters.has(cid)) hiddenClusters.delete(cid);
        else hiddenClusters.add(cid);
        renderClusterChips(clusterList);
        // Re-rendering replaced the buttons; hand focus back to the one the
        // keyboard user was on.
        clusterChipsEl.querySelector(`[data-cluster-toggle="${cid}"]`)?.focus();
        // During replay the chips speak the replay's own partition — refresh
        // the current frame instead of tearing the replay down.
        if (replay) applyReplayStep(replay.step);
        else rebuildGraph();
      });
    });
  }

  // ── Replay (the network over time) ────────────────────────────────────
  //
  // One fetch brings the whole weekly-binned pair history plus join/leave
  // stamps and a full-span community partition (colours must hold still
  // while the frames move). Each step composes a rolling 28-day window and
  // updates the sim IN PLACE: surviving nodes keep their positions and
  // velocities so the layout glides, newcomers spawn beside their strongest
  // partner, and a member with a departure stamp vanishes at the week they
  // left rather than four weeks later when their window drains.

  const REPLAY_WINDOW_BINS = 4;   // 28-day rolling window
  const REPLAY_STEP_MS = 1400;    // per week, divided by the speed picker

  let replay = null;              // null = live view
  let disposed = false;           // set by unmount; awaited work must check it

  /** Non-blocking notice over the canvas — for a failure that did NOT
   *  invalidate what is already drawn (the full-canvas veil takes a healthy
   *  graph hostage with no way to dismiss it). */
  let transientTimer = null;
  function showTransient(text) {
    rpNotice.textContent = text;
    rpNotice.hidden = false;
    if (transientTimer) clearTimeout(transientTimer);
    transientTimer = setTimeout(() => { rpNotice.hidden = true; }, 7000);
  }

  function _fmtDay(ts) {
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function applyReplayStep(step) {
    const d = replay.data;
    replay.step = step;
    rpScrub.value = String(step);
    const winStart = Math.max(0, step - (REPLAY_WINDOW_BINS - 1));
    const windowEnd = d.start + (step + 1) * d.bin_seconds;
    rpDate.textContent = `${_fmtDay(d.start + winStart * d.bin_seconds)} – ${_fmtDay(windowEnd)}`;

    // Pair weights summed over the window.
    const sums = [];
    const nodeTotal = new Map();
    for (const pr of d.pairs) {
      let w = 0;
      for (let b = winStart; b <= step; b++) w += pr.w[b];
      if (!w) continue;
      sums.push([pr.a, pr.b, w]);
      nodeTotal.set(pr.a, (nodeTotal.get(pr.a) || 0) + w);
      nodeTotal.set(pr.b, (nodeTotal.get(pr.b) || 0) + w);
    }

    // Presence: interacted in the window, cluster not hidden, and the most
    // recent membership event by the window's end isn't a departure.
    const present = new Set();
    for (const n of d.nodes) {
      if (!nodeTotal.has(n.user_id)) continue;
      if (hiddenClusters.has(n.cluster_id)) continue;
      let lastJoin = -1, lastLeave = -1;
      for (const t of n.joins) if (t <= windowEnd && t > lastJoin) lastJoin = t;
      for (const t of n.leaves) if (t <= windowEnd && t > lastLeave) lastLeave = t;
      if (lastLeave > lastJoin) continue;
      present.add(n.user_id);
    }
    const shownPairs = sums.filter(([a, b]) => present.has(a) && present.has(b));

    // Rebuild the sim arrays, carrying positions across by member id.
    const prev = new Map(nodes.map((n) => [n.id, n]));
    const meta = new Map(d.nodes.map((n) => [n.user_id, n]));
    const bestPartner = new Map();
    for (const [a, b, w] of shownPairs) {
      if (!bestPartner.has(a) || w > bestPartner.get(a)[1]) bestPartner.set(a, [b, w]);
      if (!bestPartner.has(b) || w > bestPartner.get(b)[1]) bestPartner.set(b, [a, w]);
    }
    const W = canvas.width / devicePixelRatio;
    const H = canvas.height / devicePixelRatio;
    let maxTotal = 1;
    for (const id of present) maxTotal = Math.max(maxTotal, nodeTotal.get(id) || 0);

    const nextNodes = [];
    const idx = new Map();
    for (const id of present) {
      const m = meta.get(id);
      const old = prev.get(id);
      const total = nodeTotal.get(id) || 0;
      let x, y;
      if (old) { x = old.x; y = old.y; }
      else {
        const anchor = prev.get(bestPartner.get(id)?.[0]);
        x = (anchor ? anchor.x : W / 2) + (Math.random() - 0.5) * 60;
        y = (anchor ? anchor.y : H / 2) + (Math.random() - 0.5) * 60;
      }
      idx.set(id, nextNodes.length);
      nextNodes.push({
        id,
        name: (m && m.user_name) || id,
        x, y,
        vx: old ? old.vx : 0,
        vy: old ? old.vy : 0,
        r: 6 + (total / maxTotal) * 22,
        total_outbound: total,
        total_inbound: 0,
        unique_partners: 0,
        cluster_id: (m && m.cluster_id) || 0,
      });
    }
    hovered = null;
    dragged = null;
    nodes = nextNodes;
    edges = shownPairs.map(([a, b, w]) => ({ source: idx.get(a), target: idx.get(b), weight: w }));
    labelledIdx = new Set(
      nodes
        .map((n, i) => [n.total_outbound, i])
        .sort((a, b) => b[0] - a[0])
        .slice(0, LABELLED_NODES)
        .map(([, i]) => i)
    );
    startSim();
  }

  function _rpSchedule() {
    const speed = parseInt(rpSpeed.value) || 2;
    replay.timer = setTimeout(() => {
      if (!replay || !replay.playing) return;
      if (replay.step >= replay.data.weeks - 1) { setReplayPlaying(false); return; }
      applyReplayStep(replay.step + 1);
      _rpSchedule();
    }, REPLAY_STEP_MS / speed);
  }

  function setReplayPlaying(playing) {
    if (!replay) return;
    replay.playing = playing;
    rpToggle.textContent = playing ? "⏸" : "▶";
    if (replay.timer) { clearTimeout(replay.timer); replay.timer = null; }
    if (playing) {
      // Restart from the top when play is hit at the end.
      if (replay.step >= replay.data.weeks - 1) applyReplayStep(0);
      _rpSchedule();
    }
  }

  async function enterReplay() {
    // The chip is the toggle: once a replay is running, pressing it again is
    // the same "stop" the bar's ✕ performs.
    if (replay) { exitReplay(); return; }
    replayBtn.disabled = true;
    showMessage(renderLoading("Loading the network's history…"));
    let d;
    try {
      d = await api("/api/reports/interaction-graph-series", {
        weeks: 30,
        limit: Math.max(parseInt(limitEl.value) || 40, 60),
        resolution: parseFloat(resolutionEl.value) || 1.2,
      });
    } catch (err) {
      if (disposed) return;
      replayBtn.disabled = false;
      // The live graph underneath is untouched by this failure, so say so
      // without veiling it. With no live graph, the veil is the right call.
      if (cachedData) {
        clearMessage();
        showTransient(`Couldn't load the replay — ${err.message}`);
      } else {
        showMessage(renderError(`Couldn't load the replay — ${err.message}. Press Replay to try again.`));
      }
      return;
    }
    // The panel may have been unmounted while the fetch was in flight; unmount
    // ran before `replay` existed, so nothing else would ever stop the timers.
    if (disposed) return;
    replayBtn.disabled = false;
    if (!d.nodes.length || !d.pairs.length) {
      showMessage(renderEmpty(
        "No history to replay yet — this needs a few weeks of recorded conversation between members."
      ));
      return;
    }
    clearMessage();
    if (sim) { cancelAnimationFrame(sim); sim = null; }
    replay = {
      data: d,
      step: 0,
      playing: false,
      timer: null,
      saved: {
        clusterByUser,
        clusterList,
        hiddenClusters: new Set(hiddenClusters),
        layout: currentLayout,
      },
    };
    // Replay applies no focus expansion, so the live view's focus/second-ring
    // state must not keep tinting nodes: it would paint members in a colour
    // matching no chip, against the replay's own stable-partition premise.
    focusId = null;
    secondLevelIds = new Set();
    // The replay speaks its own full-span partition: swap the cluster lookup
    // and the chips over to it, with nothing hidden to start.
    hiddenClusters.clear();
    clusterByUser = {};
    const counts = {};
    for (const n of d.nodes) {
      clusterByUser[n.user_id] = n.cluster_id;
      counts[n.cluster_id] = (counts[n.cluster_id] || 0) + 1;
    }
    renderClusterChips(
      Object.keys(counts)
        .map((cid) => ({ id: parseInt(cid), size: counts[cid] }))
        .sort((a, b) => a.id - b.id)
    );
    currentLayout = "force";   // the one layout that animates a changing graph
    replayBtn.setAttribute("aria-pressed", "true");
    replayBtn.classList.add("is-active");
    replayBtn.title = "Stop the replay and return to the live network";
    replayBar.hidden = false;
    rpScrub.max = String(d.weeks - 1);
    applyReplayStep(0);
    setReplayPlaying(true);
  }

  function exitReplay(rebuild = true) {
    if (!replay) return;
    setReplayPlaying(false);
    clusterByUser = replay.saved.clusterByUser;
    hiddenClusters.clear();
    for (const cid of replay.saved.hiddenClusters) hiddenClusters.add(cid);
    currentLayout = replay.saved.layout;
    const savedClusters = replay.saved.clusterList;
    replay = null;
    renderClusterChips(savedClusters);
    replayBar.hidden = true;
    replayBtn.setAttribute("aria-pressed", "false");
    replayBtn.classList.remove("is-active");
    replayBtn.title = "Replay the network week by week — watch people arrive, leave, and drift between groups";
    if (!rebuild) return;
    if (!cachedData) {
      // rebuildGraph would early-return and leave the last replay frame on
      // screen posing as the live graph — with the live tooltip reporting
      // window totals as "Out: N  In: 0". Blank it and say what happened.
      nodes = []; edges = []; labelledIdx = new Set();
      if (sim) { cancelAnimationFrame(sim); sim = null; }
      draw();
      showMessage(renderError(
        "The live graph didn't load. Change the period, max nodes or granularity to try again."
      ));
      return;
    }
    rebuildGraph();
  }

  replayBtn.addEventListener("click", enterReplay);
  rpClose.addEventListener("click", () => exitReplay());
  rpToggle.addEventListener("click", () => setReplayPlaying(!replay?.playing));
  rpScrub.addEventListener("input", () => {
    if (!replay) return;
    setReplayPlaying(false);
    applyReplayStep(parseInt(rpScrub.value) || 0);
  });
  rpSpeed.addEventListener("change", () => {
    // Re-arm the timer so the new speed applies to the next step, not the one
    // already scheduled.
    if (replay && replay.playing) { setReplayPlaying(false); setReplayPlaying(true); }
  });

  // ── Data loading ──────────────────────────────────────────────────────

  let cachedData = null;

  async function fetchData() {
    // Leave the replay first: it snapshots cluster state on entry and restores
    // it on exit, so exiting AFTER this installs the new dataset's clusters
    // (via rebuildGraph below) would put the previous dataset's legend and
    // cluster lookup back over fresh data.
    if (replay) exitReplay(false);
    // include_metrics=1 even though the metric tiles are gone: community
    // detection runs inside the metrics block server-side, and without it
    // every node comes back cluster_id 0 (reports_data.get_interaction_graph_data).
    const params = { limit: parseInt(limitEl.value) || 40, include_metrics: 1 };
    const d = parseInt(timescaleEl.value);
    if (!isNaN(d) && d > 0) params.days = d;
    const res = parseFloat(resolutionEl.value);
    if (!isNaN(res)) params.resolution = res;
    showMessage(renderLoading("Building the connection graph…"));
    try {
      cachedData = await api("/api/reports/interaction-graph", params);
    } catch (err) {
      // Stop the sim and blank the canvas so a stale graph can't be mistaken
      // for fresh data, then say what failed and what to do about it.
      cachedData = null;
      if (sim) { cancelAnimationFrame(sim); sim = null; }
      nodes = []; edges = [];
      draw();
      renderClusterChips([]);
      showMessage(renderError(`Couldn't load the connection graph — ${err.message}. Change the period, max nodes or granularity to try again.`));
      return;
    }
    const metrics = cachedData.metrics || null;
    clusterByUser = {};
    for (const n of cachedData.nodes || []) {
      clusterByUser[n.user_id] = n.cluster_id || 0;
    }
    renderClusterChips(metrics ? metrics.clusters : []);
    rebuildGraph();
  }

  function rebuildGraph() {
    if (replay) exitReplay(false);
    if (!cachedData) return;

    const qs = new URLSearchParams();
    qs.set("layout", layoutEl.value);
    if (timescaleEl.value) qs.set("timescale", timescaleEl.value);
    if (memberFS.getValue()) qs.set("member", memberFS.getValue());
    qs.set("min_pct", minPctEl.value);
    qs.set("layers", layersEl.value);
    qs.set("limit", limitEl.value);
    qs.set("spread", spreadEl.value);
    qs.set("resolution", resolutionEl.value);
    qs.set("max_per_node", maxPerNodeEl.value);
    if (hiddenClusters.size) qs.set("hidden_clusters", [...hiddenClusters].join(","));
    history.replaceState(null, "", `#/connection-graph?${qs}`);

    if (sim) { cancelAnimationFrame(sim); sim = null; }
    // The node array is about to be replaced; a hover or drag captured
    // against the old one would point draw()'s neighbourhood fade (and the
    // physics) at a ghost index.
    hovered = null;
    dragged = null;
    spreadMult = parseFloat(spreadEl.value) || 1.0;

    const { nodes: fNodes, pairs } = applyFilters(cachedData);

    if (!fNodes.length) {
      // Overlay, never innerHTML — replacing the wrapper would throw away the
      // canvas the mouse listeners are bound to and leave the panel inert.
      nodes = []; edges = [];
      resize();
      draw();
      showMessage(renderEmpty(
        "No connections match these filters. Widen the period, lower Min Edge %, raise Max Nodes, or clear the focus member."
      ));
      return;
    }
    clearMessage();

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
        cluster_id: n.cluster_id ?? 0,
      });
    });

    for (const p of pairs) {
      const si = nodeMap.get(p.from_id), ti = nodeMap.get(p.to_id);
      if (si !== undefined && ti !== undefined) {
        edges.push({ source: si, target: ti, weight: p.weight });
      }
    }

    // Standing labels: the most active members by total interactions.
    labelledIdx = new Set(
      nodes
        .map((n, i) => [n.total_outbound + n.total_inbound, i])
        .sort((a, b) => b[0] - a[0])
        .slice(0, LABELLED_NODES)
        .map(([, i]) => i)
    );

    currentLayout = layoutEl.value;
    if (currentLayout === "community") positionCommunity();
    else if (currentLayout === "radial") positionRadial();
    else if (currentLayout === "circular") positionCircular();
    else if (currentLayout === "hierarchical") positionHierarchical();

    startSim();
  }

  // Controls: fetch when data source changes, rebuild when filters change
  timescaleEl.addEventListener("change", fetchData);
  limitEl.addEventListener("change", fetchData);
  layoutEl.addEventListener("change", rebuildGraph);
  for (const el of [minPctEl, layersEl, maxPerNodeEl]) el.addEventListener("change", rebuildGraph);
  spreadEl.addEventListener("input", rebuildGraph);

  // Resolution slider: live-update readout, debounce server fetch
  let resolutionTimer = null;
  resolutionEl.addEventListener("input", () => {
    resolutionValEl.textContent = parseFloat(resolutionEl.value).toFixed(1);
    if (resolutionTimer) clearTimeout(resolutionTimer);
    resolutionTimer = setTimeout(fetchData, 350);
  });

  // Fullscreen toggle
  fullscreenBtn.addEventListener("click", () => {
    const fsEl = document.fullscreenElement;
    if (fsEl) {
      document.exitFullscreen();
    } else {
      wrap.requestFullscreen?.().catch(() => {});
    }
  });
  // Watch for member selection — rebuild after dropdown closes
  const memberSlot = container.querySelector('[data-slot="member"]');
  let lastMemberId = memberFS.getValue();
  memberSlot.addEventListener("focusout", () => {
    setTimeout(() => {
      const cur = memberFS.getValue();
      if (cur !== lastMemberId) { lastMemberId = cur; rebuildGraph(); }
    }, 200);
  });

  resize();
  fetchData();

  const ro = new ResizeObserver(() => { resize(); requestDraw(); });
  ro.observe(wrap);

  return {
    unmount() {
      disposed = true;
      if (transientTimer) clearTimeout(transientTimer);
      if (replay && replay.timer) clearTimeout(replay.timer);
      if (sim) cancelAnimationFrame(sim);
      ro.disconnect();
    },
  };
}
