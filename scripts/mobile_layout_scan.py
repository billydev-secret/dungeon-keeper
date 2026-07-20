"""Sweep every dashboard panel in a real browser and report horizontal-overflow faults.

Two signals, both validated against the announcement-button regression this was
built for (a flex row of controls that ran off a phone screen):

  viewport — an element's right edge extends past the viewport and no
             auto/scroll ancestor makes it reachable. This is content sticking
             out / a horizontally-scrolling page.
  clipped  — a container clips its overflow (overflow-x: hidden|clip) while
             holding content wider than itself, so children are cut off and
             unreachable. This is what the regression actually did.

A deliberately scrollable box (overflow-x: auto|scroll — a wide data table) is
NOT a fault under either signal: the user can reach the rest by scrolling.

This is the measurement/diagnostic tool. The gate lives in
tests/web/test_mobile_layout.py and shares AUDIT_JS with this file.

Run: .venv/bin/python scripts/mobile_layout_scan.py [--viewport phone] [--limit N] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from bot_modules.core.db_utils import open_db  # noqa: E402
from migrations import apply_migrations_sync  # noqa: E402
from web_server.auth import OpenAuth  # noqa: E402
from web_server.server import create_app  # noqa: E402

VIEWPORTS = {"phone": 390, "tablet": 768, "desktop": 1280}

# Shared with the gate. `el.scrollWidth > el.clientWidth + SLOP` tolerates the
# sub-pixel rounding browsers introduce so a pixel-perfect layout doesn't trip.
AUDIT_JS = r"""
(slop) => {
  const vw = document.documentElement.clientWidth;
  const out = { viewport: [], clipped: [] };
  const desc = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (el.classList.length) s += '.' + [...el.classList].slice(0, 2).join('.');
    for (const a of el.attributes) if (a.name.startsWith('data-')) { s += `[${a.name}]`; break; }
    return s;
  };
  const visible = (el, r) => {
    if (r.width <= 0 || r.height <= 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  // Only a real scrollbar (auto|scroll) makes off-edge content reachable.
  // hidden|clip does NOT — that content is gone, which is the bug we hunt.
  const reachable = (el) => {
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      const ox = getComputedStyle(n).overflowX;
      if (ox === 'auto' || ox === 'scroll') return true;
    }
    return false;
  };
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) continue;
    if (r.right > vw + 1 && !reachable(el)) {
      out.viewport.push({ sel: desc(el), by: Math.round(r.right - vw) });
    }
    const st2 = getComputedStyle(el);
    const ox = st2.overflowX;
    // `text-overflow: ellipsis` clips on purpose (a truncated label with a
    // visible …); that's a design choice, not lost content. Exempt it.
    const ellipsis = st2.textOverflow === 'ellipsis';
    if ((ox === 'hidden' || ox === 'clip') && !ellipsis
        && el.scrollWidth > el.clientWidth + slop) {
      out.clipped.push({ sel: desc(el), hides: el.scrollWidth - el.clientWidth });
    }
  }
  return out;
}
"""

# Sub-pixel slop for the clipped check (px). Small; the regression hid 87px.
CLIP_SLOP = 4


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Ctx:
    """The minimal AppContext substitute the web route tests use (see conftest)."""

    def __init__(self, db_path: Path, guild_id: int = 123):
        self.db_path = db_path
        self.guild_id = guild_id
        self.bot = None
        self._cache: dict = {}

    def open_db(self):
        return open_db(self.db_path)

    def guild_config(self, guild_id: int):
        from bot_modules.core.app_context import GuildConfig

        cfg = self._cache.get(guild_id)
        if cfg is None:
            with self.open_db() as conn:
                cfg = GuildConfig.load(
                    conn, guild_id, allow_legacy_fallback=(guild_id == self.guild_id)
                )
            self._cache[guild_id] = cfg
        return cfg

    def invalidate_guild_config(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)


def _disable_rate_limit() -> None:
    """Neutralize the per-IP rate limiter for in-process browser tests.

    Every browser request originates from the one loopback IP, so the limiter
    would 429 the sweep and bury the console/network signal we're checking. This
    patches the token check to always pass — test process only, never prod.
    """
    from web_server import server as _srv

    _srv._RateBucket.consume = lambda self: True  # type: ignore[method-assign]


def serve(db_path: Path, port: int) -> uvicorn.Server:
    """Start the dashboard on a background thread; return once it's accepting."""
    _disable_rate_limit()
    app = create_app(_Ctx(db_path), auth=OpenAuth())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.1)
    raise RuntimeError("dashboard server did not start")


def enumerate_panels(page, base: str) -> list[str]:
    """Every panel id, read from the rendered nav so new panels are covered."""
    page.goto(base, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_selector(".nav-item", timeout=30_000)
    ids = page.evaluate(
        "() => [...document.querySelectorAll('.nav-item')]"
        ".map(b => b.dataset.pageId).filter(Boolean)"
    )
    return list(dict.fromkeys(ids))


def _settle(page, tries: int = 20, interval: int = 100) -> None:
    """Block until the layout stops changing width.

    Panels render async (fetch → re-render), and some size sub-widgets from the
    container width *before* a vertical scrollbar appears — so a snapshot taken
    mid-render can miss (or invent) an overflow. Polling ``scrollWidth`` until
    two consecutive reads match makes the measurement deterministic instead of
    a race against whichever frame we happened to catch; this is what removed a
    panel that flapped clean/dirty between runs.
    """
    # Web fonts reflow text width when they arrive; wait them out first so a
    # late swap can't shift an element across the edge after we've measured.
    try:
        page.evaluate("() => document.fonts && document.fonts.ready")
    except Exception:
        pass
    prev = -1
    for _ in range(tries):
        w = page.evaluate("() => document.documentElement.scrollWidth")
        if w == prev:
            return
        prev = w
        page.wait_for_timeout(interval)


def _goto_panel(page, url: str) -> None:
    """Navigate without hanging on live panels.

    ``wait_until="networkidle"`` is a trap here: live-updating panels (live-log,
    system-stats) hold a connection open, so the network never idles and the
    nav times out — a flake that only bites some runs. Instead wait for the DOM,
    then give the network a *bounded* moment to quiet down, then let ``_settle``
    confirm the layout has stopped moving.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    try:
        page.wait_for_load_state("networkidle", timeout=2500)
    except Exception:
        pass  # a live panel that never idles — settle handles the rest


def audit_panel(page, base: str, pid: str) -> dict:
    """Navigate one panel and return its {viewport, clipped} findings.

    Assumes ``page`` belongs to a *fresh context* (see ``audit_on_fresh_context``)
    — isolation per panel is what makes the result deterministic. Waits for the
    layout width to settle before measuring.
    """
    _goto_panel(page, f"{base}/#/{pid}")
    _settle(page)
    return page.evaluate(AUDIT_JS, CLIP_SLOP)


def audit_on_fresh_context(browser, base: str, pid: str, width: int, height: int = 900) -> dict:
    """Audit one panel in a brand-new browser context, then dispose of it.

    A fresh *context* (not just a fresh page) per panel is what finally made the
    sweep deterministic: pages sharing the default context let earlier panels'
    state bleed into later ones, so borderline panels flapped clean/dirty between
    runs. Isolation costs a little startup per panel and buys a trustworthy gate.
    """
    context = browser.new_context(viewport={"width": width, "height": height})
    try:
        return audit_panel(context.new_page(), base, pid)
    finally:
        context.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--viewport", action="append", choices=list(VIEWPORTS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    viewports = args.viewport or list(VIEWPORTS)

    with TemporaryDirectory() as td:
        db_path = Path(td) / "scan.db"
        apply_migrations_sync(db_path)
        port = _free_port()
        server = serve(db_path, port)
        base = f"http://127.0.0.1:{port}"

        findings: dict[str, list] = defaultdict(list)
        counts: Counter = Counter()
        pairs: Counter = Counter()

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            ids = enumerate_panels(page, base)
            page.close()
            if args.limit:
                ids = ids[: args.limit]
            print(f"{len(ids)} panels × {len(viewports)} viewports\n")

            for vp in viewports:
                for n, pid in enumerate(ids, 1):
                    try:
                        res = audit_on_fresh_context(browser, base, pid, VIEWPORTS[vp])
                    except Exception as e:
                        findings["error"].append({"panel": pid, "viewport": vp, "err": str(e)[:200]})
                        counts["error"] += 1
                        continue
                    for kind in ("viewport", "clipped"):
                        if res[kind]:
                            counts[kind] += len(res[kind])
                            pairs[f"{kind}:{vp}"] += 1
                            findings[kind].append(
                                {"panel": pid, "viewport": vp, "items": res[kind][:6],
                                 "total": len(res[kind])}
                            )
                    print(f"  [{vp}] {n}/{len(ids)} {pid}          ", end="\r", flush=True)
                print()
            browser.close()
        server.should_exit = True

    print("\n── findings ──────────────────────────────────────")
    for kind in ("viewport", "clipped", "error"):
        hits = sum(v for k, v in pairs.items() if k.startswith(kind + ":"))
        print(f"{kind:9} {counts[kind]:5} findings across {hits} panel/viewport pairs")
    if any(pairs):
        print("\npanels with at least one finding:")
        for key in sorted(pairs):
            examples = ", ".join(
                f["panel"] for f in findings[key.split(":")[0]]
                if f["viewport"] == key.split(":")[1]
            )
            print(f"  {key:18} {pairs[key]:3}  {examples[:90]}")
    if args.json:
        args.json.write_text(json.dumps(findings, indent=2))
        print(f"\nfull findings → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
