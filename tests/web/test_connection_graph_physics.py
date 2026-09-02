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

  const opts = { width: W, height: H };
  const out = { MAX_NODE_SPEED: P.MAX_NODE_SPEED, SETTLED_SPEED: P.SETTLED_SPEED };
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
