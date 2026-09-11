"""Browser gate for the connection graph's force layout
(``js/panels/connection-graph-physics.js``).

Todo #171 — "the replay feature of the community graph never settles, just
jumps around". The replay was *not* re-seeding the layout: ``applyReplayStep``
carries every surviving node's position and velocity across weekly frames on
purpose. The layout simply never converged, for two reasons, both reproduced
below against the pre-fix constants:

  1. Repulsion was ``8000 / (d² + 1)`` with no floor under ``d``. Two dots that
     drift into each other collapse to ~1px apart, and 1/d² at 1px launches the
     pair at ~3,900 px/frame — off-stage, back through the pack, and out again.
     A 60-node graph never reached ``SETTLED_SPEED`` in 3,000 frames.
     ``test_overlapping_pair_is_not_launched`` and ``test_dense_graph_settles``
     both fail on the old code.
  2. Convergence took 400–2,400 ticks even when it stayed out of that hole,
     while a replay step lasts 700ms — about 42 animation frames. Every week
     was drawn a few percent of the way to equilibrium, so what moved on screen
     was the settling, not the change between weeks.
     ``test_replay_step_is_settled_before_it_is_drawn`` covers this.

The replay asks for ``max(limit, 60)`` nodes while the live view defaults to
40, which is why Billy saw it in the replay first — but the live graph sits on
the same cliff, so the fix is in the shared model rather than gated on replay
(Billy's call, 2026-09-01).

**The row reopened (2026-09-11), and this file is why it could.** Both of
``settle()``'s bounds were under-sized, so the 09-01 fix turned "never settles
at all" into "settles on a quiet week, still jumps on a busy one" — load- and
churn-dependent, which is why it read as intermittent. The tests above could
not see it for two reasons, both now covered:

  * they measured 60-node weeks built as *survivors plus newcomers*, so a week
    with 25 joiners was a 85-node graph. A real week replaces departures: the
    total stays at whatever the replay asked for, and ``limit`` went up to the
    Max Nodes ceiling of 100, whose worst week needs 2,466 ticks against a
    bound of 2,000.
  * ``budgetMs`` was 250ms against a worst week costing ~715ms, so the wall
    clock — not the tick count — was the bound that actually stopped the loop
    on a busy week. The ``replay`` spec below freezes the clock on purpose (see
    its comment), which is right for a layout assertion and is exactly why no
    test here could ever have noticed.

``test_the_worst_replay_week_converges_within_the_tick_bound`` and
``test_the_replay_asks_for_a_fixed_number_of_nodes`` both fail on the 09-01
code. The tick counts they assert are integer arithmetic over a seeded graph,
so they mean the same thing on any machine.

The module is imported directly rather than through the mounted panel: the
physics has no DOM in it, and driving it through a canvas would test the
canvas. One wiring assertion at the bottom checks the panel actually calls it.

Marked ``browser``. Auto-skips without Playwright / Chromium.
"""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

playwright_sync = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright not installed (pip install playwright && playwright install chromium)",
)

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from mobile_layout_scan import _goto_panel, serve  # noqa: E402

from tests.db_template import migrated_db  # noqa: E402

_PANEL = (
    Path(__file__).resolve().parents[2]
    / "src/web_server/static/js/panels/connection-graph.js"
)


def _chromium_available() -> bool:
    try:
        with playwright_sync.sync_playwright() as pw:
            path = pw.chromium.executable_path
            return bool(path) and Path(path).exists()
    except Exception:
        return False


if not _chromium_available():
    pytest.skip(
        "Chromium not installed — run `python -m playwright install chromium`",
        allow_module_level=True,
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, tmp: Path):
        db = tmp / "connection-graph-physics.db"
        # Module-scoped, so the per-test reaper must not delete it mid-run.
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("connection-graph-physics"))
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", srv.port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def browser(dashboard) -> Iterator[object]:
    with playwright_sync.sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def page(browser, dashboard):
    context = browser.new_context(viewport={"width": 1100, "height": 800})
    pg = context.new_page()
    _goto_panel(pg, f"{dashboard.base}/")
    yield pg
    context.close()


# ── The harness evaluated in the page ────────────────────────────────────
#
# A seeded LCG builds the graph so every run gets the same one: a physics
# regression that only shows on one random seed is not a gate.

_HARNESS = """
async (spec) => {
  const P = await import('/static/js/panels/connection-graph-physics.js');
  const W = 900, H = 560;

  let seed = spec.seed || 1;
  const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff;
                      return seed / 0x7fffffff; };

  // Nodes on a ring, exactly how the live view seeds a fresh graph.
  const build = (n, deg, maxW) => {
    const nodes = [], edges = [], seen = new Set();
    for (let i = 0; i < n; i++) nodes.push({ x: 0, y: 0, vx: 0, vy: 0, r: 6 + rnd() * 22 });
    const want = Math.round(n * deg / 2);
    let guard = 0;
    while (edges.length < want && guard++ < want * 50) {
      const a = Math.floor(rnd() * n), b = Math.floor(rnd() * n);
      if (a === b) continue;
      const k = a < b ? `${a}-${b}` : `${b}-${a}`;
      if (seen.has(k)) continue;
      seen.add(k);
      edges.push({ source: a, target: b, weight: 1 + Math.floor(rnd() * maxW) });
    }
    const sp = Math.min(W, H) * 0.30;
    nodes.forEach((nd, i) => {
      const a = (i / n) * Math.PI * 2;
      nd.x = W / 2 + Math.cos(a) * sp + (rnd() - 0.5) * 30;
      nd.y = H / 2 + Math.sin(a) * sp + (rnd() - 0.5) * 30;
    });
    return { nodes, edges };
  };

  // A week as the replay actually composes one: the graph holds `total` nodes,
  // of which `joiners` are new this week and the rest carry their settled
  // position and velocity over. Newcomers spawn at the canvas centre, which is
  // what makes a high-churn week expensive — they all start on top of each
  // other, in the middle, and have to be pushed out through the pack.
  //
  // The distinction from `build(total) + joiners` matters: that builds
  // total + joiners nodes, a graph the Max Nodes ceiling does not allow, and
  // it was how the 09-01 measurements came out optimistic.
  const week = (total, joiners, deg, maxW, s) => {
    // Reseed per week, so repeated calls in one evaluate compose the SAME week
    // rather than walking the generator on and measuring a different graph each
    // time. Without this the truncation sweep below compares stopping points
    // across different graphs, which measures nothing.
    seed = s;
    const survivors = total - joiners;
    const { nodes, edges } = build(survivors, deg, maxW);
    P.settle(nodes, edges, { width: W, height: H, maxTicks: 8000, budgetMs: 1e9, now: () => 0 });
    for (let i = 0; i < joiners; i++) {
      nodes.push({ x: W / 2 + (rnd() - 0.5) * 60, y: H / 2 + (rnd() - 0.5) * 60,
                   vx: 0, vy: 0, r: 6 + rnd() * 22 });
      edges.push({ source: nodes.length - 1, target: Math.floor(rnd() * survivors),
                   weight: 1 + Math.floor(rnd() * maxW) });
    }
    return { nodes, edges };
  };

  const opts = { width: W, height: H };
  const out = {
    MAX_NODE_SPEED: P.MAX_NODE_SPEED,
    SETTLED_SPEED: P.SETTLED_SPEED,
    SETTLE_MAX_TICKS: P.SETTLE_MAX_TICKS,
    SETTLE_BUDGET_MS: P.SETTLE_BUDGET_MS,
  };
  // tick() reports |vx|+|vy| — that is the unit SETTLED_SPEED has always been
  // in. The cap is on the true magnitude, so measure that separately instead
  // of comparing one against the other (they differ by up to √2).
  const fastestStep = (nodes) => Math.max(...nodes.map((n) => Math.hypot(n.vx, n.vy)));

  if (spec.kind === 'overlap') {
    // Two big dots dropped almost exactly on top of each other — the state the
    // old 1/d² repulsion turned into a 3,900 px/frame launch.
    const nodes = [
      { x: 450, y: 280, vx: 0, vy: 0, r: 28 },
      { x: 450.5, y: 280.5, vx: 0, vy: 0, r: 28 },
    ];
    const gap = () => Math.hypot(nodes[0].x - nodes[1].x, nodes[0].y - nodes[1].y);
    const before = gap();
    let peak = 0;
    for (let i = 0; i < spec.ticks; i++) {
      P.tick(nodes, [], opts);
      peak = Math.max(peak, fastestStep(nodes));
      if (i === 0) out.firstTickGain = gap() - before;
    }
    out.peak = peak;
    out.separation = Math.hypot(nodes[0].x - nodes[1].x, nodes[0].y - nodes[1].y);
    out.maxOffset = Math.max(...nodes.map((n) => Math.hypot(n.x - W / 2, n.y - H / 2)));
    return out;
  }

  if (spec.kind === 'settles') {
    const { nodes, edges } = build(spec.n, spec.deg, spec.maxW);
    let peak = 0, settledAt = -1, slowest = Infinity;
    for (let i = 0; i < spec.ticks; i++) {
      const s = P.tick(nodes, edges, opts);
      peak = Math.max(peak, fastestStep(nodes));
      slowest = Math.min(slowest, s);
      if (settledAt < 0 && s <= P.SETTLED_SPEED) { settledAt = i; break; }
    }
    out.peak = peak;
    out.slowest = slowest;
    out.settledAt = settledAt;
    return out;
  }

  if (spec.kind === 'replay') {
    // One replay step: settle a week, then compose the next one the way
    // applyReplayStep does — survivors keep x/y/vx/vy, newcomers spawn at the
    // canvas centre — and settle that before it would be drawn.
    const { nodes, edges } = build(spec.n, spec.deg, spec.maxW);
    P.settle(nodes, edges, { ...opts, maxTicks: 4000, budgetMs: 10000 });
    for (let i = 0; i < spec.newcomers; i++) {
      nodes.push({ x: W / 2 + (rnd() - 0.5) * 60, y: H / 2 + (rnd() - 0.5) * 60,
                   vx: 0, vy: 0, r: 6 + rnd() * 22 });
      edges.push({ source: nodes.length - 1, target: Math.floor(rnd() * spec.n),
                   weight: 1 + Math.floor(rnd() * spec.maxW) });
    }
    // A frozen clock: the wall-clock bound in settle() is a hang guard, and
    // letting it bind here would make the assertion a benchmark of the CI box.
    // On a loaded runner that is exactly what happened — the burst was cut off
    // mid-rearrange and the frame drifted, failing a test that passed locally.
    const res = P.settle(nodes, edges, { ...opts, now: () => 0, ...(spec.settleOpts || {}) });
    out.burstTicks = res.ticks;
    out.burstSpeed = res.speed;
    // What the animation loop is left holding. The frame does not have to be
    // dead still when it is painted — it has to be close enough that the rest
    // is a short glide that finishes before the next week arrives.
    const from = nodes.map((n) => ({ x: n.x, y: n.y }));
    let glidePeak = 0;
    for (let i = 0; i < spec.animationFrames; i++) {
      P.tick(nodes, edges, opts);
      glidePeak = Math.max(glidePeak, fastestStep(nodes));
    }
    out.glidePeak = glidePeak;
    // How far the worst node actually travels while the frame is on screen —
    // the direct measure of "it jumps around instead of settling".
    out.glideDrift = Math.max(...nodes.map((n, i) => Math.hypot(n.x - from[i].x, n.y - from[i].y)));
    return out;
  }

  if (spec.kind === 'budget') {
    const { nodes, edges } = build(spec.n, spec.deg, spec.maxW);
    let clock = 0;
    // A fake clock: real wall time makes the assertion a benchmark of the CI
    // box, which is how a timing test becomes flaky.
    const now = () => (clock += spec.msPerTick);
    const res = P.settle(nodes, edges, { ...opts, budgetMs: spec.budgetMs, maxTicks: 100000, now });
    out.ticks = res.ticks;
    out.settled = res.settled;
    return out;
  }

  if (spec.kind === 'week') {
    // Ticks to convergence for one real week, clock frozen so only the tick
    // count can stop the loop — the figure is then arithmetic, not a benchmark.
    const { nodes, edges } = week(spec.total, spec.joiners, spec.deg, spec.maxW, spec.seed);
    const res = P.settle(nodes, edges, { ...opts, maxTicks: 8000, budgetMs: 1e9, now: () => 0 });
    out.ticks = res.ticks;
    out.settled = res.settled;
    // Ticks the PRODUCTION defaults would allow it — no maxTicks/budgetMs
    // override, so a change to either constant shows up here.
    const again = week(spec.total, spec.joiners, spec.deg, spec.maxW, spec.seed);
    const prod = P.settle(again.nodes, again.edges, { ...opts, now: () => 0 });
    out.prodTicks = prod.ticks;
    out.prodSettled = prod.settled;
    return out;
  }

  if (spec.kind === 'truncation') {
    // One week, settled repeatedly with the burst stopped at each of `caps`,
    // measuring how far the worst dot then travels over the frame's time on
    // screen. Converged is the last entry.
    out.rows = spec.caps.map((cap) => {
      const { nodes, edges } = week(spec.total, spec.joiners, spec.deg, spec.maxW, spec.seed);
      const res = P.settle(nodes, edges, { ...opts, maxTicks: cap, budgetMs: 1e9, now: () => 0 });
      const from = nodes.map((n) => ({ x: n.x, y: n.y }));
      for (let i = 0; i < spec.animationFrames; i++) P.tick(nodes, edges, opts);
      return {
        cap,
        ticks: res.ticks,
        settled: res.settled,
        drift: Math.max(...nodes.map((n, i) => Math.hypot(n.x - from[i].x, n.y - from[i].y))),
      };
    });
    return out;
  }

  if (spec.kind === 'dragged') {
    const { nodes, edges } = build(spec.n, spec.deg, spec.maxW);
    const held = nodes[0];
    const before = { x: held.x, y: held.y };
    for (let i = 0; i < spec.ticks; i++) P.tick(nodes, edges, { ...opts, dragged: held });
    out.moved = Math.hypot(held.x - before.x, held.y - before.y);
    return out;
  }

  throw new Error('unknown spec ' + spec.kind);
}
"""


def _run(page, **spec):
    return page.evaluate(_HARNESS, spec)


# ── Defect 1: the repulsion singularity ──────────────────────────────────


def test_overlapping_pair_is_not_launched(page):
    """Two dots on top of each other push apart; they do not get fired off-stage.

    The old model peaked near 3,900 px/frame here and threw both nodes clear
    of a 900×560 canvas in a single tick.
    """
    r = _run(page, kind="overlap", ticks=300)

    assert r["peak"] <= r["MAX_NODE_SPEED"] + 1e-6, (
        f"a node moved {r['peak']:.0f} px in one tick — the repulsion "
        f"singularity is back (cap is {r['MAX_NODE_SPEED']})"
    )
    # They still separate — the floor must not turn repulsion off.
    assert r["separation"] > 40, r["separation"]
    # And it separates them promptly. Flooring the direction vector as well as
    # the magnitude (the first cut) made the push scale with how close they
    # already were, so a nearly-coincident pair crawled apart instead of being
    # pushed: 0.5px apart moved 0.04px in the first tick, against 1.9px once
    # the direction is a proper unit vector.
    assert r["firstTickGain"] > 1.0, (
        f"an overlapping pair only gained {r['firstTickGain']:.2f}px in the first tick — "
        "the repulsion direction is being scaled by the floor, not normalised"
    )
    # And neither one is flung off the canvas on the way.
    assert r["maxOffset"] < 500, f"a node reached {r['maxOffset']:.0f}px from centre"


# ── Defect 2: convergence ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("n", "deg", "max_w"),
    [
        pytest.param(40, 6, 400, id="live-default-40-nodes"),
        # What the replay actually asks for: enterReplay requests
        # max(limit, 60). The old model never settled at this size.
        pytest.param(60, 6, 400, id="replay-60-nodes"),
        pytest.param(60, 6, 100, id="replay-60-nodes-light-weights"),
        pytest.param(80, 6, 400, id="max-nodes-raised"),
    ],
)
def test_dense_graph_settles(page, n, deg, max_w):
    r = _run(page, kind="settles", n=n, deg=deg, maxW=max_w, ticks=3000)

    assert r["settledAt"] >= 0, (
        f"{n} nodes never reached SETTLED_SPEED in 3000 ticks "
        f"(fastest node still moving at {r['peak']:.1f} px/frame)"
    )
    assert r["peak"] <= r["MAX_NODE_SPEED"] + 1e-6, r["peak"]


# One replay step at the default speed is REPLAY_STEP_MS / 2 = 700ms, which is
# about 42 animation frames. A week that has not stopped moving by then is
# still travelling when the next one replaces it — which is the whole bug.
_STEP_FRAMES = 42


@pytest.mark.parametrize(
    "newcomers",
    [0, 3, 8, 25],
    ids=["steady", "few-joins", "many-joins", "heavy-churn"],
)
def test_replay_step_comes_to_rest_within_its_own_step(page, newcomers):
    """A composed week stops moving before the next one arrives.

    This is #171 in the shape it happens: the step starts from last week's
    settled positions, newcomers land at the canvas centre, and what is left
    after the settle burst has to be a short glide — not a journey the next
    week interrupts. On the old code this never came to rest at all.
    """
    r = _run(
        page, kind="replay", n=60, deg=6, maxW=400,
        newcomers=newcomers, animationFrames=_STEP_FRAMES,
    )

    # Drift over the whole step is the direct reading of the complaint: a dot
    # that wanders further than its own diameter while the week is on screen
    # has not settled. Pre-fix this ran to hundreds of pixels per frame.
    assert r["glideDrift"] < 5, (
        f"a dot travelled {r['glideDrift']:.0f}px during the {_STEP_FRAMES} frames the "
        f"week is on screen (burst: {r['burstTicks']} ticks, ended at "
        f"{r['burstSpeed']:.2f} px/frame) — the frame is still moving when the next replaces it"
    )
    assert r["glidePeak"] < 0.5, f"nodes still moving {r['glidePeak']:.2f} px/frame after the burst"


def test_settle_stops_at_its_budget(page):
    """The burst is bounded, so a graph that will not converge can't hang the tab."""
    r = _run(page, kind="budget", n=80, deg=10, maxW=400, budgetMs=24, msPerTick=1)

    assert not r["settled"]
    # One tick of progress minimum, and it stops at the budget rather than
    # running to maxTicks (100,000 here).
    assert 1 <= r["ticks"] <= 25, r["ticks"]
    # The clock advances 1ms per reading, so the burst breaks after the 24th
    # tick. The count it reports has to be the ticks actually RUN: the
    # budget-break path used to return the loop index (23), undercounting by
    # the tick that had already executed when the budget was noticed.
    assert r["ticks"] == 24, (
        f"24ms of budget at 1ms/tick is 24 executed ticks, got {r['ticks']} — "
        "settle() is reporting the loop index rather than the work done"
    )


# ── Defect 3 (2026-09-11): the bounds on the burst were under-sized ──────
#
# Every figure below comes from a 90-week sweep — churn from 0 to 50% of the
# graph, ten seeds each — over the same seeded builder the harness uses. The
# worst 60-node week is 24 joiners on seed 1; the worst 100-node week is 40
# joiners on seed 42. Those two weeks are what these tests drive.
#
# The degree and weight spread (8 and 438) are the live graph's, not the
# defaults used above: the weeks a replay actually composes are denser than a
# synthetic deg-6 graph, and the settle cost is in the density.
_WORST_WEEK = dict(deg=8, maxW=438)
_WORST_60 = dict(total=60, joiners=24, seed=1, **_WORST_WEEK)
_WORST_100 = dict(total=100, joiners=40, seed=42, **_WORST_WEEK)


def test_the_worst_replay_week_converges_within_the_tick_bound(page):
    """A busy week finishes settling, and finishes with room to spare.

    This is the one that reopened todo #171. The 09-01 bound was 2,000 ticks
    and the worst 60-node week needs 1,975 — it cleared by 1.3%, which is not a
    margin, and at the node limit the replay used to request it did not clear
    at all. A week that does not converge is drawn mid-rearrange, and a frame
    drawn mid-rearrange travels while it is on screen (see the test below).
    """
    r = _run(page, kind="week", **_WORST_60)

    assert r["settled"], f"the worst 60-node week never converged ({r['ticks']} ticks)"
    assert r["prodSettled"], (
        f"the worst 60-node week needs {r['ticks']} ticks and the production "
        f"bound is {r['SETTLE_MAX_TICKS']} — the frame gets drawn half-settled"
    )
    # Not merely inside the bound: inside it with somewhere to go. A graph the
    # sweep didn't happen to hit must not be a cliff edge.
    assert r["ticks"] <= r["SETTLE_MAX_TICKS"] * 0.8, (
        f"the worst week uses {r['ticks']} of {r['SETTLE_MAX_TICKS']} ticks — "
        f"over 80% of the bound leaves nothing for a week the sweep missed"
    )


def test_the_replay_node_cap_is_what_makes_that_bound_reachable(page):
    """Why the replay stopped honouring Max Nodes.

    The dial goes to 100, and the replay used to ask for `max(dial, 60)`. The
    settle cost is superlinear in node count, so the worst week at the ceiling
    needs 2,466 ticks — more than the bound, and it is paid once per week, 30
    times a playback. This documents the gap the cap exists to close: raise the
    replay's node count again and this test says what it costs.
    """
    r = _run(page, kind="week", **_WORST_100)

    assert r["ticks"] > 2000, (
        f"a 100-node week now converges in {r['ticks']} ticks — the force model "
        f"got cheaper, so the replay's node cap may be worth revisiting"
    )
    # The capped week is the comparison: same churn fraction, far less work.
    capped = _run(page, kind="week", **_WORST_60)
    assert capped["ticks"] < r["ticks"], (capped["ticks"], r["ticks"])


def test_a_burst_cut_short_is_not_a_proportionally_better_frame(page):
    """The reason the bound has to reach convergence rather than approach it.

    Spending more ticks does not buy a stiller picture — the layout passes
    through its own rearrangements on the way, so drift over the frame's time on
    screen rises and falls as the burst is stopped later and later. Only the
    converged frame is reliable. A replay whose weeks land on random points of
    that curve is the "jumps around" in the row's own words, and it is why a
    wall-clock bound is the wrong shape for this: it stops the loop at whatever
    point the machine's load puts it at.
    """
    caps = [100, 200, 300, 500, 700, 900, 1200, 1600, 1900, 3000]
    r = _run(
        page, kind="truncation", caps=caps, animationFrames=_STEP_FRAMES, **_WORST_60
    )
    rows = r["rows"]
    converged = [row for row in rows if row["settled"]]
    cut = [row for row in rows if not row["settled"]]
    assert converged, "no cap in the sweep reached convergence — the week got harder"
    assert len(cut) >= 6, f"only {len(cut)} cut-short bursts to compare"

    # The converged frame holds still; the worst cut-short one does not, by an
    # order of magnitude. Measured: 1.3px converged against 61px at 200 ticks.
    assert converged[0]["drift"] < 5, converged[0]
    worst_cut = max(row["drift"] for row in cut)
    assert worst_cut > 25, (
        f"the worst cut-short burst only drifted {worst_cut:.0f}px — if stopping "
        f"the burst early has stopped mattering, these bounds can be revisited"
    )

    # And it is not monotonic, which is the part that makes a partial burst
    # worthless rather than merely worse: at least one later stopping point is
    # further from rest than an earlier one.
    inversions = [
        (a["ticks"], a["drift"], b["ticks"], b["drift"])
        for a, b in zip(cut, cut[1:])
        if b["drift"] > a["drift"]
    ]
    assert inversions, (
        "drift now falls monotonically as the burst runs longer, so a "
        f"time-bounded burst would degrade gracefully: {[(x['ticks'], round(x['drift'], 1)) for x in cut]}"
    )


def test_the_wall_clock_cannot_become_the_working_bound(page):
    """``budgetMs`` is a hang guard; ``maxTicks`` is the bound that must bind.

    A burst stopped by the clock makes the picture depend on how loaded the
    machine is — and by the test above, a partial burst is not a partial
    improvement, so that is not a graceful degradation but a random one. The
    two constants together say how slow a client has to be before the clock
    takes over: budget ÷ ticks is the per-tick cost at which it starts binding.
    Pure arithmetic over the two exported values, so it holds anywhere.

    At the 09-01 values (250ms / 2000 ticks) the clock took over at 0.125
    ms/tick, against a measured 0.078 on a developer laptop — a 1.6x margin,
    which a background tab or a phone eats whole.
    """
    r = _run(page, kind="week", total=60, joiners=0, seed=1, **_WORST_WEEK)
    ms_per_tick = r["SETTLE_BUDGET_MS"] / r["SETTLE_MAX_TICKS"]
    assert ms_per_tick >= 0.3, (
        f"the wall clock starts cutting bursts short at {ms_per_tick:.3f} ms/tick; "
        f"a 60-node tick measures about 0.078 ms, so this leaves only "
        f"{ms_per_tick / 0.078:.1f}x for a slower client. Raise SETTLE_BUDGET_MS "
        f"with SETTLE_MAX_TICKS, or the tick bound stops being the real one."
    )


def test_a_held_node_is_not_moved_by_physics(page):
    """Dragging survives the fix — the cap must not nudge the node under the cursor."""
    r = _run(page, kind="dragged", n=40, deg=6, maxW=400, ticks=200)
    assert r["moved"] == 0, r["moved"]


# ── Wiring ───────────────────────────────────────────────────────────────


def test_replay_step_calls_the_settle_burst():
    """The physics fix is worthless if the replay path doesn't use it."""
    src = _PANEL.read_text(encoding="utf-8")
    step = src[src.index("function applyReplayStep(") : src.index("REPLAY_MIN_HOLD_MS")]
    assert "physicsSettle(" in step, (
        "applyReplayStep no longer settles the frame before drawing it — "
        "the replay is back to animating its convergence (todo #171)"
    )


def test_the_replay_asks_for_a_fixed_number_of_nodes():
    """The Max Nodes dial must not reach the replay's node count.

    ``enterReplay`` asked for ``Math.max(parseInt(limitEl.value) || 40, 60)``,
    so a dial at its ceiling handed the replay a graph whose worst week cannot
    settle inside the burst's bound — thirty times over, once per week. The
    live graph keeps the dial; it settles once.
    """
    src = _PANEL.read_text(encoding="utf-8")
    assert "const REPLAY_NODES = 60;" in src, (
        "REPLAY_NODES is gone — the replay's node count is back to being "
        "whatever the Max Nodes dial says (todo #171)"
    )
    start = src.index("async function enterReplay()")
    body = src[start : src.index("function exitReplay(")]
    assert "limit: REPLAY_NODES," in body, "the replay no longer pins its node count"
    assert "limitEl" not in body, (
        "enterReplay reads the Max Nodes dial again — the settle cost per week "
        "is superlinear in node count and is paid 30 times per playback"
    )


def test_dragging_the_scrubber_does_not_settle_every_week_it_crosses():
    """A burst per scrub event freezes the tab for seconds.

    The range emits one ``input`` per week the thumb crosses — ~26 across a
    30-week history — and each settle burst can run to its wall-clock guard.
    A drag has to recompose only; the frame the drag is *released* on is the
    one worth settling, and that is what ``change`` is for.
    """
    src = _PANEL.read_text(encoding="utf-8")
    start = src.index('rpScrub.addEventListener("input"')
    end = src.index("rpSpeed.addEventListener")
    handlers = src[start:end]

    on_input = handlers[: handlers.index('rpScrub.addEventListener("change"')]
    assert "settle: false" in on_input, (
        "the scrubber's input handler settles every week it crosses — a drag "
        "across the history runs a burst per event and locks the main thread"
    )
    assert 'rpScrub.addEventListener("change"' in handlers, (
        "nothing settles the frame the drag lands on"
    )
