// The connection graph's force layout, lifted out of the panel so it can be
// exercised directly (tests/web/test_connection_graph_physics.py) instead of
// only through a mounted canvas.
//
// Why it moved (2026-09-01, todo #171 — "the replay never settles, just jumps
// around"): the replay carries node positions and velocities across weekly
// frames, so the layout was never being re-seeded. It simply never converged.
// Two defects, both measured against this exact code:
//
//   1. Repulsion was `REPULSION / (d² + 1)` with no floor under `d`. Two dots
//      that drift into each other collapse to ~1px apart, which is a 1/d²
//      singularity: the pair launched at up to 3,900 px/frame, crossed the
//      canvas, fell back through the pack and relaunched. A 60-node graph
//      never settled at all, over 3,000 frames.
//   2. Even when it stayed out of that hole, the layout needed 400–2,400
//      frames to reach SETTLED_SPEED, while a replay step lasts 700ms — 42
//      frames at 60fps. Every week's frame was shown a few percent of the way
//      to equilibrium, so what moved on screen was the *settling*, not the
//      change between weeks.
//
// The floor and the cap below fix (1) — peak speed drops from ~3,900 to ~42
// px/frame — and `settle()` fixes (2) by paying the convergence cost up front
// when a replay frame is composed.
//
// The replay requests `max(limit, 60)` nodes while the live view defaults to
// 40, which is why this showed up in the replay first; the live graph sits on
// the same cliff and goes over it whenever Max Nodes is raised.

export const BASE_REPULSION = 8000;
export const SPRING_K = 0.005;
export const BASE_SPRING_LEN = 120;
export const DAMPING = 0.85;
export const GRAVITY = 0.02;
export const COMM_GRAVITY = 0.08; // pull toward community center

/** Below this the fastest node is treated as stopped and the loop can end. */
export const SETTLED_SPEED = 0.05;

/** Furthest a node may travel in one tick. A node crosses the canvas over
 *  many frames or not at all — this is what stops a close pair from being
 *  flung off-stage before the next tick can pull it back. */
export const MAX_NODE_SPEED = 30;

/** Separation floor for repulsion, when the two dots' radii aren't known. */
const FALLBACK_RADIUS = 10;

/**
 * Advance the simulation one step, mutating `nodes` in place.
 *
 * @param {Array} nodes  {x, y, vx, vy, r} — mutated
 * @param {Array} edges  {source, target, weight} as indices into `nodes`
 * @param {Object} opts
 *   - isCommunity: repel/spring within a community only, gravitate to its centre
 *   - communityOf: node index → community id  (community layout)
 *   - commCenters: community id → {x, y}      (community layout)
 *   - spreadMult: the panel's Spread dial
 *   - width, height: canvas size in CSS pixels
 *   - dragged: a node object the cursor owns, which physics must not move
 * @returns {number} the fastest node's speed, so a caller can stop when settled
 */
export function tick(nodes, edges, opts = {}) {
  const {
    isCommunity = false,
    communityOf = {},
    commCenters = {},
    spreadMult = 1,
    width = 0,
    height = 0,
    dragged = null,
  } = opts;

  const cxC = width / 2;
  const cyC = height / 2;
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
      // Floor the separation at the distance where the two dots touch. Inside
      // that they are drawn overlapping anyway, so there is nothing to gain
      // from a force that keeps climbing — and everything to lose, since it
      // is the climb that launches them.
      //
      // Only the MAGNITUDE is floored. The direction still divides by the real
      // distance, so it stays a unit vector: flooring both (the first cut of
      // this fix) left the push scaling with how far apart the pair already
      // was, which decays to nothing as they converge — the opposite of
      // "push apart firmly". They did still separate, just limply.
      const touching = (ni.r || FALLBACK_RADIUS) + (nj.r || FALLBACK_RADIUS);
      const force = REPULSION / Math.max(dist2, touching * touching);
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

    let vx = (ni.vx + fx) * DAMPING;
    let vy = (ni.vy + fy) * DAMPING;
    const speed = Math.sqrt(vx * vx + vy * vy);
    if (speed > MAX_NODE_SPEED) {
      vx = (vx / speed) * MAX_NODE_SPEED;
      vy = (vy / speed) * MAX_NODE_SPEED;
    }
    ni.vx = vx;
    ni.vy = vy;
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

/**
 * Run the simulation to equilibrium before anything is drawn, bounded so a
 * pathological graph can't hang the tab.
 *
 * This is what a replay step uses: composing a week's frame moves the
 * equilibrium, and the point of the replay is to show *where the network
 * ended up* that week, not to watch it converge for the 700ms the frame is on
 * screen. A step starts from the previous week's settled positions, so the
 * perturbation is only as big as that week's arrivals and departures — a quiet
 * week returns in a few ticks, a busy one spends most of the budget.
 *
 * @returns {{ticks: number, settled: boolean, speed: number}}
 */
export function settle(nodes, edges, opts = {}) {
  // Bounds sized from the measurement in todo #171, on a 60-node graph (what
  // the replay asks for) with a week's worth of joiners spawning into it:
  //
  //   joiners   ticks to settle   worst drift over the step
  //         0                 1                     0.4 px
  //         3               687                     0.7 px
  //         8             1,285                     0.7 px
  //        25             1,501                     1.4 px
  //
  // The burst has to run to the threshold to get that. Cutting it off part-way
  // does NOT give a proportionally better frame — the layout is mid-rearrange,
  // so a 190-tick burst leaves ~38px of drift and a 600-tick one can leave
  // more than a 300-tick one. Hence a tick bound generous enough to reach
  // convergence rather than a tight one: the cost is up to ~220ms of a 700ms
  // step on a busy week, and nothing at all on a quiet one, which is the price
  // of a week that actually holds still.
  //
  // The wall clock is a hang guard, not the working bound — it is deliberately
  // slack enough that a normal client never reaches it, because a time-bound
  // burst makes the *picture* depend on how loaded the machine is. A client
  // slow enough to hit it gets a softer frame instead of a stall.
  const maxTicks = opts.maxTicks ?? 2000;
  const budgetMs = opts.budgetMs ?? 250;
  const now = opts.now ?? (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
  const started = now();
  let speed = 0;
  // Counts ticks actually executed, so both exits report the same thing — the
  // loop index alone undercounts the budget-break path by the tick that had
  // already run when the budget was noticed.
  let ticks = 0;
  while (ticks < maxTicks) {
    speed = tick(nodes, edges, opts);
    ticks++;
    if (speed <= SETTLED_SPEED) return { ticks, settled: true, speed };
    // Checked after the tick so the burst always makes at least one step of
    // progress, however tight the budget.
    if (now() - started >= budgetMs) break;
  }
  return { ticks, settled: false, speed };
}
