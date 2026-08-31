"""Browser gate for ``channelName``/``roleName`` in ``js/config-helpers.js``
(S3 — deleted channels and roles rendering as raw snowflake ids).

Both helpers look a saved id up in the guild's live channel/role list and
build a display label. A deleted channel or role is never coming back — it
is absent from the list forever, not just until the next fetch — so before
this fix the ``ch ? ... : id`` fallback handed back the bare snowflake with
no indication anything was wrong. Billy saw this on the Cleanup and Games
Global Config panels: rows full of bare numbers with no way to tell "this
channel was deleted" from "this is some other kind of value".

The fix: a not-found id renders as ``⚠ Missing channel/role (id …)`` — the
same wording ``_danglingOption`` already uses for the ``<select>`` builders
just above these two functions in the same file, so the dashboard has one
visual language for "this stored id doesn't resolve". The id stays in the
text on purpose: two different deleted channels must still read as two
different rows on an audit log, not collapse into the same "gone" label.

Both helpers are used ~20 places across ~10 panels, several of which
interpolate the return value straight into ``innerHTML`` with no ``esc()``
of their own (docs.js, role-menus.js) — so the fix has to stay a plain
string with no markup in it, exactly like the resolved-name case already is.

The module is mounted directly rather than through a panel, so nothing here
depends on a panel's data (the test env runs no bot, so channel/role fetches
would 503).

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
        db = tmp / "config-helpers-missing.db"
        # Module-scoped, so the per-test reaper must not delete it mid-run.
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("config-helpers-missing"))
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
def page(browser, dashboard):
    context = browser.new_context(viewport={"width": 1100, "height": 800})
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)[:200]))
    _goto_panel(pg, f"{dashboard.base}/")
    pg.__dict__["_dk_errors"] = errors
    yield pg
    context.close()


_LIVE_ROLE = "1000000000000000001"
_DEAD_ROLE = "1487814583300128941"
_DEAD_ROLE_2 = "1487814583300128942"
_ROLES = [{"id": _LIVE_ROLE, "name": "Denizen"}]

_LIVE_CHAN = "1000000000000000002"
_DEAD_CHAN = "1525522177951137954"
_DEAD_CHAN_2 = "1525522177951137955"
_CHANNELS = [{"id": _LIVE_CHAN, "name": "general", "type": "text"}]

_NAME_LOOKUP = """
async ({ fn, list, id }) => {
  const cfg = await import('/static/js/config-helpers.js');
  return cfg[fn](list, id);
}
"""


def _name(page, fn, entities, id_):
    return page.evaluate(_NAME_LOOKUP, {"fn": fn, "list": entities, "id": id_})


# ── channelName ────────────────────────────────────────────────────────────


def test_channel_name_resolves_a_live_channel(page):
    assert _name(page, "channelName", _CHANNELS, _LIVE_CHAN) == "#general"


def test_channel_name_unset_reads_as_disabled(page):
    """`0` is a real value meaning "no channel configured" — never flagged."""
    assert _name(page, "channelName", _CHANNELS, "0") == "(disabled)"


def test_channel_name_flags_a_deleted_channel(page):
    """The bug: a channel absent from the list rendered as the bare id, with
    nothing to tell an admin "deleted" apart from "some other number"."""
    out = _name(page, "channelName", _CHANNELS, _DEAD_CHAN)
    assert "⚠" in out
    assert _DEAD_CHAN in out


def test_channel_name_missing_label_has_no_markup(page):
    """docs.js and role-menus.js interpolate this straight into innerHTML with
    no esc() of their own — it must stay plain text, not become an XSS hole."""
    out = _name(page, "channelName", _CHANNELS, _DEAD_CHAN)
    assert "<" not in out and ">" not in out


def test_two_deleted_channels_stay_distinguishable(page):
    """A moderator reading an audit row full of deleted channels needs to be
    able to tell two different ones apart — the whole point of keeping the id
    in the label rather than collapsing everything to e.g. "(deleted)"."""
    a = _name(page, "channelName", _CHANNELS, _DEAD_CHAN)
    b = _name(page, "channelName", _CHANNELS, _DEAD_CHAN_2)
    assert a != b
    assert _DEAD_CHAN in a and _DEAD_CHAN not in b
    assert _DEAD_CHAN_2 in b and _DEAD_CHAN_2 not in a


# ── roleName ─────────────────────────────────────────────────────────────


def test_role_name_resolves_a_live_role(page):
    assert _name(page, "roleName", _ROLES, _LIVE_ROLE) == "@Denizen"


def test_role_name_unset_reads_as_none(page):
    assert _name(page, "roleName", _ROLES, "0") == "(none)"


def test_role_name_flags_a_deleted_role(page):
    out = _name(page, "roleName", _ROLES, _DEAD_ROLE)
    assert "⚠" in out
    assert _DEAD_ROLE in out


def test_role_name_missing_label_has_no_markup(page):
    out = _name(page, "roleName", _ROLES, _DEAD_ROLE)
    assert "<" not in out and ">" not in out


def test_two_deleted_roles_stay_distinguishable(page):
    a = _name(page, "roleName", _ROLES, _DEAD_ROLE)
    b = _name(page, "roleName", _ROLES, _DEAD_ROLE_2)
    assert a != b
    assert _DEAD_ROLE in a and _DEAD_ROLE not in b
    assert _DEAD_ROLE_2 in b and _DEAD_ROLE_2 not in a
