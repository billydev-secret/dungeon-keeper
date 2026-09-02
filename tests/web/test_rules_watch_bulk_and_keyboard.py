"""Rules Watch bulk selection + keyboard triage (see docs/plans, "rip through it
real fast" — Billy's own words for the ask).

Two features, one panel:

  * bulk — row checkboxes + "Select all shown" feed a confirm-gated bulk-label
    action that must post *exactly* the ids the moderator checked, never every
    id the current filter happens to show;
  * keyboard — Up/Down move the open event, V/F label it and (via the existing
    ``nextUnlabeledId``) auto-advance, and all of it goes inert the moment
    focus is inside a text field.

Each test claims one ``priority_tier`` bucket (immediate / digest / logged)
for its own seeded events and drives the panel through that tier's filter
button, so the three tests never read each other's rows regardless of
execution order — no shared "current count" assumption, and no dependency on
tests running in file order.

Marked ``browser``; auto-skips without Playwright/Chromium. The server and
browser fixtures copy test_tabs_widget.py's shape (OpenAuth, a fresh migrated
DB, a module-scoped uvicorn instance) — seeding happens once, before the
server starts, so every test opens the same long-lived dashboard.
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
from mobile_layout_scan import serve  # noqa: E402

from tests.db_template import migrated_db  # noqa: E402
from bot_modules.core.db_utils import open_db  # noqa: E402
from bot_modules.rules_watch import service  # noqa: E402


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


# _Ctx (mobile_layout_scan.py) defaults to guild_id=123 for OpenAuth/LAN mode,
# which is what get_active_guild_id() falls back to with no session cookie.
GUILD = 123


def _event(conn, *, tier: str, detected_at: float, message_id: int) -> int:
    """One unlabeled event. ``detected_at`` controls list order within a tier
    filter (get_pending_events sorts a tiered query by detected_at DESC only —
    no priority_score tiebreak — so distinct values make row order
    deterministic)."""
    return service.insert_event(
        conn,
        guild_id=GUILD,
        message_id=message_id,
        author_id=900 + message_id,
        channel_id=42,
        detected_at=detected_at,
        guard_verdict="violation",
        guard_rule="3",
        guard_confidence=0.8,
        priority_score=5.0,
        priority_tier=tier,
    )


def _seed(db_path: Path) -> dict[str, int]:
    """Nine events, three per tier — one reserved bucket per test below."""
    ids: dict[str, int] = {}
    with open_db(db_path) as conn:
        # immediate — test_bulk_selection_state_...
        ids["a1"] = _event(conn, tier="immediate", detected_at=3000, message_id=1)
        ids["a2"] = _event(conn, tier="immediate", detected_at=2000, message_id=2)
        ids["a3"] = _event(conn, tier="immediate", detected_at=1000, message_id=3)
        # digest — test_bulk_action_posts_only_the_selected_ids
        ids["b1"] = _event(conn, tier="digest", detected_at=3000, message_id=4)
        ids["b2"] = _event(conn, tier="digest", detected_at=2000, message_id=5)
        ids["b3"] = _event(conn, tier="digest", detected_at=1000, message_id=6)
        # logged — test_keyboard_shortcut_labels_and_advances_but_not_while_typing
        ids["c1"] = _event(conn, tier="logged", detected_at=3000, message_id=7)
        ids["c2"] = _event(conn, tier="logged", detected_at=2000, message_id=8)
        ids["c3"] = _event(conn, tier="logged", detected_at=1000, message_id=9)
    return ids


class _Server:
    def __init__(self, tmp: Path):
        db = tmp / "rules-watch-bulk.db"
        migrated_db(db, reap=False)
        self.ids = _seed(db)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("rules-watch-bulk"))
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


@pytest.fixture()
def page(browser) -> Iterator[object]:
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    yield p
    ctx.close()


def _open_tier(page, base: str, tier: str, count: int) -> None:
    page.goto(f"{base}/#/rules-watch?tier={tier}", wait_until="domcontentloaded", timeout=15000)
    page.wait_for_function(
        "n => document.querySelectorAll('.rw-row').length === n", arg=count, timeout=10_000,
    )


def _row_checkbox(event_id: int) -> str:
    return f'.rw-row[data-id="{event_id}"] [data-select]'


# ── A. bulk selection state ─────────────────────────────────────────────────

def test_bulk_selection_state_tracks_checkboxes_and_select_all(dashboard, page):
    ids = dashboard.ids
    _open_tier(page, dashboard.base, "immediate", 3)

    # Nothing selected on load: count reads zero, both bulk actions disabled —
    # if the panel ever seeded selectedIds from stale state this would catch it.
    assert page.inner_text("[data-selected-count]") == "0 selected"
    assert page.is_disabled('[data-bulk-label="true"]')
    assert page.is_disabled('[data-bulk-label="false"]')

    page.check(_row_checkbox(ids["a1"]))
    page.check(_row_checkbox(ids["a2"]))
    assert page.inner_text("[data-selected-count]") == "2 selected"
    assert not page.is_disabled('[data-bulk-label="true"]')
    assert not page.is_disabled('[data-bulk-label="false"]')

    # Checking a row's box must not also open it — that's the click-guard this
    # regresses without: removing it makes selectEvent() fire on every check
    # and this assertion fails (the detail pane would show event #a2 instead).
    assert "Select an event to review" in page.inner_text("[data-detail]")

    # Select all shown — including the one never manually checked.
    page.check("[data-select-all]")
    assert page.inner_text("[data-selected-count]") == "3 selected"
    assert page.is_checked(_row_checkbox(ids["a3"]))

    # ...and back off again.
    page.uncheck("[data-select-all]")
    assert page.inner_text("[data-selected-count]") == "0 selected"
    assert not page.is_checked(_row_checkbox(ids["a1"]))
    assert page.is_disabled('[data-bulk-label="true"]')


# ── B. bulk action posts the ids actually selected ──────────────────────────

def test_bulk_action_posts_only_the_selected_ids(dashboard, page):
    ids = dashboard.ids
    _open_tier(page, dashboard.base, "digest", 3)

    # Check b1 and b3, deliberately skipping b2 — the request must reflect
    # exactly that, not "every digest row currently on screen".
    page.check(_row_checkbox(ids["b1"]))
    page.check(_row_checkbox(ids["b3"]))

    with page.expect_request(
        lambda r: r.url.endswith("/api/rules-watch/events/bulk-label") and r.method == "POST"
    ) as req_info:
        page.once("dialog", lambda d: d.accept())
        page.click('[data-bulk-label="false"]')
    body = req_info.value.post_data_json

    assert body["event_ids"] == [ids["b1"], ids["b3"]], (
        "the bulk request must carry only the checked ids, in queue order — "
        "sending every visible id would silently sweep up b2 too"
    )
    assert body["is_violation"] is False

    # Report honestly: a clean sweep names the count and direction plainly.
    page.wait_for_selector(".toast", timeout=5000)
    assert "Labeled 2 event" in page.inner_text(".toast")

    # Unlabeled Only is on by default, so the two just-labeled rows fall out
    # of the reloaded queue — only b2 remains.
    page.wait_for_function("document.querySelectorAll('.rw-row').length === 1", timeout=5000)
    assert page.get_attribute(".rw-row", "data-id") == str(ids["b2"])

    # A partial sweep must say so, not claim success outright — stub the
    # server's reply (the skip logic itself is covered at the service layer;
    # this checks the panel actually surfaces `skipped_count`).
    page.route(
        "**/api/rules-watch/events/bulk-label",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"labeled":[%d],"labeled_count":1,'
            '"skipped":[999999],"skipped_count":1}' % ids["b2"],
        ),
    )
    page.check(_row_checkbox(ids["b2"]))
    page.once("dialog", lambda d: d.accept())
    page.click('[data-bulk-label="true"]')
    page.wait_for_function(
        "() => document.querySelectorAll('.toast').length > 0 "
        "&& [...document.querySelectorAll('.toast')].some(t => t.textContent.includes('skipped'))",
        timeout=5000,
    )
    toast_texts = " | ".join(page.locator(".toast").all_inner_texts())
    assert "1 skipped" in toast_texts, (
        f"a partial sweep must name the skipped count, not read as a clean "
        f"success: {toast_texts!r}"
    )


# ── C. keyboard triage: label + advance, inert while typing ────────────────

def test_keyboard_shortcut_labels_and_advances_but_not_while_typing(dashboard, page):
    ids = dashboard.ids
    _open_tier(page, dashboard.base, "logged", 3)

    label_requests: list[str] = []

    def _track(req):
        if req.method == "POST" and "/label" in req.url and "bulk-label" not in req.url:
            label_requests.append(req.url)

    page.on("request", _track)

    # Open c1 and confirm it — 'V' must hit the same endpoint the button does.
    page.click(f'.rw-row[data-id="{ids["c1"]}"]')
    page.wait_for_selector(f'.rw-detail__header:has-text("Event #{ids["c1"]}")')
    with page.expect_request(f"**/api/rules-watch/events/{ids['c1']}/label"):
        page.keyboard.press("v")

    # nextUnlabeledId walks forward from c1 and finds c2 — the queue drains
    # under the keypress with no click in between.
    page.wait_for_selector(f'.rw-detail__header:has-text("Event #{ids["c2"]}")', timeout=5000)
    assert len(label_requests) == 1

    # The correction field is a real text input — typing "v" into it must not
    # also fire the shortcut. Without the isTypingTarget guard this sends a
    # second /label request right here and the next assertion fails.
    page.click("[data-corrected-rule]")
    page.keyboard.type("v")
    assert page.input_value("[data-corrected-rule]") == "v"
    page.keyboard.press("f")
    page.wait_for_timeout(200)  # give a wrongly-fired request a moment to land
    assert len(label_requests) == 1, "a shortcut fired while focus was in a text field"
    assert "Event #" + str(ids["c2"]) in page.inner_text(".rw-detail__header"), (
        "focus should still be on c2 — a stray keystroke must not have advanced it"
    )

    # Move focus off the field, then the same key works: false-positive c2,
    # advance to c3.
    page.click(".rw-hint")
    with page.expect_request(f"**/api/rules-watch/events/{ids['c2']}/label"):
        page.keyboard.press("f")
    page.wait_for_selector(f'.rw-detail__header:has-text("Event #{ids["c3"]}")', timeout=5000)
    assert len(label_requests) == 2

    # Arrow navigation moves the open event without labeling anything.
    page.keyboard.press("ArrowUp")
    page.wait_for_timeout(200)
    assert len(label_requests) == 2, "ArrowUp must only move focus, never label"
