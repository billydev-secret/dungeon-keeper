"""The Warnings panel can act on a warning, not just display one.

The panel was read-only for its whole life while ``POST .../revoke`` sat
wired to nothing, so a mod had to copy a numeric id out of the dashboard and
back into ``/revokewarn``. These pin the three things about the button row
that can regress without looking broken in a screenshot:

  * an active warning offers Revoke; a revoked one does not, because the
    endpoint 409s on a second revoke and there is nothing left to revoke;
  * Delete is admin-only in the UI as well as on the server — a moderator
    must not be shown a button whose request can only ever 403;
  * Revoke actually completes end to end, prompt included, and the card
    comes back saying who revoked it.

Marked ``browser``. Auto-skips without Playwright / Chromium.
"""

from __future__ import annotations

import json
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
ACTIVE = "Reposting the same link everywhere"
REVOKED = "Handled last month and taken back"
# The end-to-end test actually revokes, so it gets a row of its own rather
# than leaning on running after the read-only tests that share this database.
TO_REVOKE = "Argued with a mod in the general channel"


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
        db = tmp / "warning-actions.db"
        migrated_db(db, reap=False)
        now = time.time()
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason,"
            " created_at, revoked) VALUES (?,?,?,?,?,0)",
            (_GUILD, 800001, 900123, ACTIVE, now - 3600),
        )
        con.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason,"
            " created_at, revoked) VALUES (?,?,?,?,?,0)",
            (_GUILD, 800003, 900123, TO_REVOKE, now - 1800),
        )
        con.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason,"
            " created_at, revoked, revoked_at, revoked_by, revoke_reason)"
            " VALUES (?,?,?,?,?,1,?,?,?)",
            (_GUILD, 800002, 900123, REVOKED, now - 90000, now - 86400, 900123,
             "Wrong call on my part"),
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
    srv = _Server(tmp_path_factory.mktemp("warning-actions"))
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


def _open_warning(browser, base: str, reason: str, *, as_moderator: bool = False):
    """Pick the warning by its reason text, not its row index.

    The queue sorts newest-first today; if that changes, selecting by index
    would fail complaining about the wrong button rather than the sort.
    """
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    if as_moderator:
        # app.js fills window.__dk_user from /api/me after load, so setting the
        # global up front is clobbered. Patch the response and drop only
        # "admin" — stripping the payload would cost the panel the moderator
        # permission it needs to render at all.
        def _demote(route):
            payload = route.fetch().json()
            payload["perms"] = [p for p in payload.get("perms", []) if p != "admin"]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        page.route("**/api/me", _demote)
    page.goto(f"{base}/#/mod-warnings", wait_until="domcontentloaded", timeout=20_000)
    page.wait_for_selector(".ticket-item", timeout=30_000)
    page.locator(".ticket-item", has_text=reason[:30]).first.click()
    page.wait_for_selector(".td-body", timeout=15_000)
    return page


def _action_buttons(page) -> list[str]:
    return page.evaluate(
        "() => [...document.querySelectorAll('.ticket-detail .act-btn')]"
        ".map(b => b.dataset.action)"
    )


def test_active_warning_offers_revoke_and_delete_to_an_admin(browser, dashboard):
    page = _open_warning(browser, dashboard.base, ACTIVE)
    try:
        assert _action_buttons(page) == ["revoke", "delete"]
    finally:
        page.close()


def test_revoked_warning_has_no_revoke_button(browser, dashboard):
    """A second revoke would 409 — the button must be gone, not merely fail."""
    page = _open_warning(browser, dashboard.base, REVOKED)
    try:
        assert _action_buttons(page) == ["delete"]
    finally:
        page.close()


def test_moderator_is_not_shown_delete(browser, dashboard):
    page = _open_warning(browser, dashboard.base, ACTIVE, as_moderator=True)
    try:
        assert _action_buttons(page) == ["revoke"]
    finally:
        page.close()


def test_revoke_goes_through_and_the_card_updates(browser, dashboard):
    """End to end: click, answer the optional prompt, see the card change."""
    page = _open_warning(browser, dashboard.base, TO_REVOKE)
    try:
        page.locator('.ticket-detail .act-btn[data-action="revoke"]').click()
        page.wait_for_selector(".confirm-overlay input", timeout=10_000)
        page.locator(".confirm-overlay input").fill("Sorted it out with them")
        page.locator(".confirm-overlay [data-confirm]").click()
        # The panel refetches, so the card is rebuilt from the server's answer.
        page.wait_for_selector(".ticket-detail .badge-dim", timeout=15_000)
        body = page.locator(".ticket-detail").inner_text()
        assert "Revoked" in body
        assert "Sorted it out with them" in body
        assert _action_buttons(page) == ["delete"]
    finally:
        page.close()
