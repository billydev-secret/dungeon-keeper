"""The perk shop was one 1,339-line page; these are the reasons it is three.

Each assertion here pins a defect that co-location caused, not a layout
preference — all three would come back silently if the pages were re-merged or
a gate were "tidied up", and none of them makes a page look broken.

  * The emoji approval queue must stay reachable by an economy manager. Its
    routes are gated ``require_economy_manager``, so the backend grants that
    role access — but the queue used to live on an ``adminOnly`` page, which
    denied it. The nav gate and the route gate have to agree.

  * The priced dials must be the only form on their page. ``guardForm``'s
    unsaved-edits flag is a module global in config-helpers.js, and
    ``showStatus(el, true, …)`` clears it. The old page had 41 showStatus call
    sites, so approving an emoji — or merely *starting* an image upload —
    cleared the warning protecting a half-typed hoard-tax rate.

  * ``economy-sinks`` must keep resolving. Route ids are frozen; the page still
    exists, so it is not a MOVED_PAGES case and must simply still work.
"""

from __future__ import annotations

import re
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JS = _ROOT / "src" / "web_server" / "static" / "js"
_PANELS = _JS / "panels"
_ROUTES = _ROOT / "src" / "web_server" / "routes"


def _code_only(path: Path) -> str:
    """Panel source with comments stripped.

    These files explain the split in their header comments, so they *talk about*
    forms and catalogs at length. Counting `<form` across the raw text therefore
    measures the prose, not the markup — which is exactly how the first version
    of the two assertions below failed against correct code. Block comments and
    whole-line `//` comments go; an inline `//` inside a URL string is left
    alone, since it never starts a line.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(
        ln for ln in text.splitlines() if not re.match(r"\s*(//|\*)", ln)
    )


def _nav_entry(page_id: str) -> str:
    """The single SECTIONS line declaring a page, straight out of app.js."""
    app = (_JS / "app.js").read_text(encoding="utf-8")
    hits = [ln for ln in app.splitlines() if f'id: "{page_id}"' in ln]
    assert len(hits) == 1, f"expected one nav entry for {page_id}, found {len(hits)}"
    return hits[0]


# ── the access bug that forced the split ────────────────────────────────────


def test_the_approvals_queue_is_not_admin_only():
    """An economy manager must be able to reach the queue the API grants them."""
    entry = _nav_entry("shop-approvals")
    assert "adminOnly" not in entry, (
        "shop-approvals is adminOnly again — the emoji queue's routes are gated "
        "require_economy_manager, so this locks out exactly the role the backend "
        "was written to admit. That was the bug that split this page."
    )


def test_the_emoji_queue_routes_still_grant_the_manager_role():
    """The other half of the same contract, so the two cannot drift apart."""
    src = (_ROUTES / "economy_manager.py").read_text(encoding="utf-8")
    for path in ("/economy/emoji-submissions", "/economy/emoji-submissions/{submission_id}/approve"):
        idx = src.find(f'"{path}"')
        assert idx != -1, f"route {path} is gone"
        window = src[idx : idx + 600]
        assert "require_economy_manager" in window, (
            f"{path} no longer admits the economy-manager role — if that is "
            f"deliberate, shop-approvals should become adminOnly to match"
        )


def test_the_comparable_queues_agree():
    """Claims and QOTD were already manager-visible; approvals now matches them."""
    for page_id in ("economy-claims", "economy-qotd-submissions", "shop-approvals"):
        assert "adminOnly" not in _nav_entry(page_id), page_id


# ── the shared-dirty-bit bug ────────────────────────────────────────────────


def test_the_priced_dials_are_alone_on_their_page():
    """One form on the page means nothing else can clear the global dirty flag."""
    src = _code_only(_PANELS / "pricing.js")
    forms = re.findall(r"<form\b", src)
    assert len(forms) == 1, f"pricing.js declares {len(forms)} forms; expected 1"
    guards = re.findall(r"\bguardForm\(", src)
    assert len(guards) == 1, f"pricing.js calls guardForm {len(guards)} times; expected 1"
    submits = re.findall(r'type="submit"', src)
    assert len(submits) == 1, f"pricing.js has {len(submits)} submit buttons; expected 1"


@pytest.mark.parametrize(
    "filename",
    ["pricing.js", "shop-approvals.js", "economy-sinks.js"],
)
def test_no_page_carries_another_page_s_concern(filename):
    """A quick smell test that the three did not re-merge by accident."""
    src = _code_only(_PANELS / filename)
    # Markers that identify each page's own subject matter.
    prices = "PRICE_FIELDS" in src
    queues = "data-emoji-queue" in src or "data-orders" in src
    catalogs = "/api/economy/icon-catalog" in src or "/api/economy/color-catalog" in src
    owned = {
        "pricing.js": (prices, not queues, not catalogs),
        "shop-approvals.js": (not prices, queues, not catalogs),
        "economy-sinks.js": (not prices, not queues, catalogs),
    }[filename]
    assert all(owned), (
        f"{filename} holds prices={prices} queues={queues} catalogs={catalogs} — "
        f"the three concerns are back on one page"
    )


# ── the frozen id ───────────────────────────────────────────────────────────


def test_the_old_id_still_resolves():
    """`economy-sinks` is frozen. The page still exists, so no redirect is owed."""
    app = (_JS / "app.js").read_text(encoding="utf-8")
    assert 'id: "economy-sinks"' in app
    moved = re.search(r"const MOVED_PAGES = \{([^}]*)\}", app, re.S)
    assert moved, "MOVED_PAGES is gone"
    assert "economy-sinks" not in moved.group(1), (
        "economy-sinks was added to MOVED_PAGES, but the page still exists — a "
        "redirect would make its own nav entry unreachable"
    )


# ── it all still mounts ─────────────────────────────────────────────────────

playwright_sync = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")

_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from mobile_layout_scan import serve, _goto_panel, _settle  # noqa: E402

from tests.db_template import migrated_db  # noqa: E402


def _chromium() -> bool:
    try:
        with playwright_sync.sync_playwright() as pw:
            path = pw.chromium.executable_path
            return bool(path) and Path(path).exists()
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[object]:
    if not _chromium():
        pytest.skip("Chromium not installed")
    tmp = tmp_path_factory.mktemp("shop-split")
    db = tmp / "split.db"
    migrated_db(db, reap=False)
    port = _free_port()
    srv = serve(db, port)
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


@pytest.mark.browser
@pytest.mark.parametrize(
    ("page_id", "heading"),
    [
        pytest.param("pricing", "Pricing", id="pricing"),
        pytest.param("economy-sinks", "Shop & Perks", id="shop"),
        pytest.param("shop-approvals", "Approvals", id="approvals"),
    ],
)
def test_each_page_mounts_under_its_own_heading(dashboard, page_id, heading):
    with playwright_sync.sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            _goto_panel(page, f"{dashboard}/#/{page_id}")
            _settle(page)
            page.wait_for_timeout(600)
            title = page.evaluate(
                "() => (document.querySelector('#panel-root .panel h2')?.textContent || '').trim()"
            )
            assert title == heading, f"{page_id} rendered {title!r}"
            assert not errors, errors
        finally:
            b.close()
