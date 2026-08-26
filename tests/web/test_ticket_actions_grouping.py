"""The Tickets action row groups by what each action acts on.

Seven controls sit at the bottom of a ticket, and they operate on two different
objects: some change the *ticket's* state, and some write a permanent
moderation record against a *member*. Presented flat, that distinction was
invisible — and because Jail carried a solid red fill, the loudest control on
the page was the destructive one.

Three properties are worth pinning, because each of them can regress silently
into something that still looks fine in a screenshot:

  * the two groups exist and say which object they act on;
  * exactly one button per pane is the filled primary, and it is never a
    destructive action — it is whatever the obvious next step is, which depends
    on the ticket's state (Claim when nobody holds it);
  * Jail is outlined, not filled. Solid-red styling belongs on the confirm
    dialog, where a decision is actually being taken, not on a button you pass
    your mouse over while reading a queue.

Marked ``browser``. Auto-skips without Playwright / Chromium.
"""

from __future__ import annotations

import socket
import sqlite3
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

_GUILD = 123


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
        db = tmp / "ticket-actions.db"
        migrated_db(db, reap=False)
        now = time.time()
        con = sqlite3.connect(db)
        # One unclaimed and one claimed-by-someone-else, so the state-dependent
        # primary has both branches to land on.
        con.execute(
            "INSERT INTO tickets (guild_id, user_id, channel_id, description,"
            " status, claimer_id, escalated, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (_GUILD, 800001, 700001, "Reposting the same link everywhere",
             "open", None, 0, now - 3600),
        )
        con.execute(
            "INSERT INTO tickets (guild_id, user_id, channel_id, description,"
            " status, claimer_id, escalated, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (_GUILD, 800002, 700002, "Someone else is already on this one",
             "open", 900123, 0, now - 7200),
        )
        con.commit()
        con.close()
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("ticket-actions"))
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


UNCLAIMED = "Reposting the same link everywhere"
HELD_BY_OTHER = "Someone else is already on this one"


def _open_ticket(browser, base: str, subject: str):
    """Pick the ticket by its text, not its row index.

    The queue's sort order is a product decision that may well change; a test
    that says `.nth(1)` would then fail with "expected Reassign to me, got
    Claim", which points at the button rather than at the sort. Selecting by
    subject keeps the failure pointing at whatever actually broke.
    """
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{base}/#/mod-tickets", wait_until="domcontentloaded", timeout=20_000)
    page.wait_for_selector(".ticket-item", timeout=30_000)
    page.locator(".ticket-item", has_text=subject).first.click()
    page.wait_for_selector(".td-act-groups", timeout=15_000)
    return page


def test_actions_are_split_into_ticket_and_member_groups(browser, dashboard):
    page = _open_ticket(browser, dashboard.base, UNCLAIMED)
    try:
        labels = page.evaluate(
            "() => [...document.querySelectorAll('.td-act-groups .section-label')]"
            ".map(e => e.textContent.trim().toLowerCase())"
        )
        assert len(labels) == 2, labels
        assert labels[0].startswith("this ticket"), labels
        assert labels[1].startswith("this member"), labels
    finally:
        page.close()


def test_member_name_is_not_shouted(browser, dashboard):
    """The eyebrow is uppercase; a chosen display name is not."""
    page = _open_ticket(browser, dashboard.base, UNCLAIMED)
    try:
        transform = page.evaluate(
            "() => getComputedStyle(document.querySelector('.td-act-who')).textTransform"
        )
        assert transform == "none", (
            f"member name is being transformed to {transform!r} — a display name "
            "must render as the person typed it"
        )
    finally:
        page.close()


def test_exactly_one_primary_and_it_is_not_destructive(browser, dashboard):
    page = _open_ticket(browser, dashboard.base, UNCLAIMED)
    try:
        info = page.evaluate(
            """() => {
              const primaries = [...document.querySelectorAll('.td-act-groups .act-btn.primary')];
              return {
                count: primaries.length,
                labels: primaries.map(b => b.textContent.trim()),
                actions: primaries.map(b => b.dataset.action),
              };
            }"""
        )
        assert info["count"] == 1, info
        assert info["actions"][0] not in ("jail", "jail-custom", "warn"), info
        # Nobody holds this one, so claiming it is the obvious next step.
        assert info["actions"][0] == "claim", info
        assert info["labels"][0] == "Claim", info
    finally:
        page.close()


def test_primary_follows_the_ticket_state(browser, dashboard):
    """A ticket someone else holds offers taking it over, not claiming it fresh."""
    page = _open_ticket(browser, dashboard.base, HELD_BY_OTHER)
    try:
        label = page.evaluate(
            "() => document.querySelector('.td-act-groups .act-btn.primary').textContent.trim()"
        )
        assert label == "Reassign to me", label
    finally:
        page.close()


def test_jail_is_outlined_not_filled(browser, dashboard):
    """Destructive styling belongs on the confirm dialog, not the queue."""
    page = _open_ticket(browser, dashboard.base, UNCLAIMED)
    try:
        style = page.evaluate(
            """() => {
              const b = document.querySelector('.act-btn.danger[data-action="jail"]');
              if (!b) return null;
              const cs = getComputedStyle(b);
              return { bg: cs.backgroundColor, color: cs.color };
            }"""
        )
        assert style is not None, "no outlined danger Jail button found"
        # rgba(...,0) or 'transparent' — either way, not a solid fill.
        assert "rgba" in style["bg"] and style["bg"].rstrip(")").endswith(" 0"), style
    finally:
        page.close()
