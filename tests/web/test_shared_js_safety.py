"""Browser gate for the shared frontend layer (js/table.js, js/config-helpers.js,
js/md-preview.js).

These three modules are imported by most of the ~130 panels, so a defect in any
of them is a defect almost everywhere. The bugs guarded here all come from the
2026-08-06 website deep review:

  * **S1 — stored XSS through ``table.js``.** The table interpolated both the
    raw cell value and any ``format()`` return straight into ``innerHTML``.
    Six report panels feed it *raw Discord display names*, which are
    member-controlled, so a nickname of ``<img src=x onerror=…>`` executed in
    the moderator's session on every page view. Cells are text by default now,
    with an explicit ``html: true`` per-column opt-in for the handful of
    columns whose whole point is a colored ``<span>``.
  * **S2 — cross-guild data bleed.** ``config-helpers.js`` memoizes
    ``/api/config`` and every ``/api/meta/*`` list in module globals, but all
    of it is scoped to the *active* guild and a guild switch re-mounts panels
    without reloading the page. ``resetMetaCaches()`` is what app.js calls to
    drop them.
  * **the shared member-picker options.** ``toSortedMemberOptions`` /
    ``memberNameLookup`` were private copies inside config-prune and
    config-inactive; now that one implementation feeds both exemption pickers,
    its ordering and labelling are pinned here rather than in either panel.
  * **md-preview link href.** ``[text](url)`` put the *text* in the ``href``,
    which both produced the wrong link and forfeited the https-only validation
    the pattern does on the URL.

Everything is exercised against the shipped modules, mounted directly rather
than through a panel, so no assertion depends on a panel's data (the test env
runs no bot, so channel/role fetches would 503).

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
        db = tmp / "shared-js.db"
        # Module-scoped, so the per-test reaper must not delete it mid-run.
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("shared-js"))
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


# ── table.js ─────────────────────────────────────────────────────────────

# The nickname a member can actually set. 32 characters is plenty; `onerror`
# on a broken image needs no user interaction at all, so merely *rendering*
# the table is the whole exploit.
_EVIL_NAME = '<img src=x onerror="window.__pwned = true">'

_RENDER_TABLE = """
async ({ rows, columns }) => {
  document.body.innerHTML = '<div id="host"></div>';
  window.__pwned = false;
  const mod = await import('/static/js/table.js');
  // Column specs cross the Playwright boundary as data, so `format` is sent as
  // a source string and revived here.
  const cols = columns.map((c) => ({
    ...c,
    format: c.format ? new Function('return ' + c.format)() : undefined,
  }));
  mod.renderSortableTable(document.getElementById('host'), {
    columns: cols, data: rows, defaultSort: columns[0].key,
  });
  const td = document.querySelector('#host td');
  return {
    text: td.textContent,
    html: td.innerHTML,
    injectedImgs: document.querySelectorAll('#host img').length,
    injectedSpans: document.querySelectorAll('#host span').length,
    pwned: window.__pwned === true,
  };
}
"""


def test_table_escapes_a_malicious_display_name(page):
    """A member-controlled nickname must render as TEXT, not as an element.

    This is S1. It fails against the pre-fix table.js: the `<img>` lands in the
    DOM, fires `onerror`, and sets the flag — stored XSS in a moderator's
    session, triggered by nothing more than opening the report.
    """
    out = page.evaluate(
        _RENDER_TABLE,
        {
            "rows": [{"user_name": _EVIL_NAME, "user_id": "123"}],
            "columns": [
                {
                    "key": "user_name",
                    "label": "Member",
                    "format": "(v, r) => r.user_name || r.user_id",
                },
            ],
        },
    )
    assert out["injectedImgs"] == 0, "format() output was parsed as HTML"
    assert not out["pwned"], "injected onerror handler executed"
    assert out["text"] == _EVIL_NAME, "the name must still be readable, verbatim"
    assert "&lt;img" in out["html"]


def test_table_escapes_the_unformatted_value_too(page):
    """The `raw ?? ""` path is the other half of S1.

    interaction-graph builds `pair_name` ("A ↔ B") out of two display names and
    renders it with no `format` at all, so escaping only the format path would
    have left that column exploitable.
    """
    out = page.evaluate(
        _RENDER_TABLE,
        {
            "rows": [{"pair_name": f"{_EVIL_NAME} ↔ someone"}],
            "columns": [{"key": "pair_name", "label": "Pair"}],
        },
    )
    assert out["injectedImgs"] == 0
    assert not out["pwned"]
    assert out["text"].startswith(_EVIL_NAME)


def test_table_escapes_a_column_label(page):
    """Labels are developer constants — except interaction-graph's, which is
    `% of ${userName}'s total`, i.e. the same untrusted display name."""
    page.evaluate(
        _RENDER_TABLE,
        {
            "rows": [{"n": 1}],
            "columns": [{"key": "n", "label": f"% of {_EVIL_NAME}'s total"}],
        },
    )
    assert page.evaluate("() => document.querySelectorAll('#host th img').length") == 0
    assert page.evaluate("() => window.__pwned === true") is False


def test_html_opt_in_still_renders_markup(page):
    """The escape-by-default must not silently blank the columns that mean it.

    `html: true` is what quality-score's colored score, retention's drop
    percentages, xp-leaderboard's ± figure and channels' sentiment/trend spans
    rely on — every one of them interpolates a computed number, never a name.
    """
    out = page.evaluate(
        _RENDER_TABLE,
        {
            "rows": [{"score": 0.82}],
            "columns": [
                {
                    "key": "score",
                    "label": "Score",
                    "html": True,
                    "format": '(v) => `<span style="color:#7F8F3A">${(v * 100).toFixed(1)}</span>`',
                },
            ],
        },
    )
    assert out["injectedSpans"] == 1, "html: true column was escaped to text"
    assert out["text"] == "82.0"


def test_html_opt_in_is_per_column_not_global(page):
    """One opting-in column must not lift escaping off its neighbours."""
    out = page.evaluate(
        _RENDER_TABLE,
        {
            "rows": [{"user_name": _EVIL_NAME, "score": 0.5}],
            "columns": [
                {"key": "user_name", "label": "Member"},
                {
                    "key": "score",
                    "label": "Score",
                    "html": True,
                    "format": "(v) => `<span>${v}</span>`",
                },
            ],
        },
    )
    assert out["injectedImgs"] == 0
    assert not out["pwned"]
    assert out["injectedSpans"] == 1


# ── config-helpers.js: the guild-switch cache reset (S2) ─────────────────

# Stubs fetch so the two "guilds" are distinguishable without a bot, then walks
# the sequence a guild switch produces: load guild A's channels, reset, load
# again. The assertion is on what a *picker* offers, because that is the thing
# that used to lie — and the thing a save then wrote to the wire.
_GUILD_SWITCH = """
async ({ reset }) => {
  document.body.innerHTML = '<div id="host"><span data-slot></span></div>';
  let guild = 'A';
  const CHANNELS = {
    A: [{ id: '1000000000000000001', name: 'alpha-general', type: 'text' }],
    B: [{ id: '2000000000000000002', name: 'bravo-general', type: 'text' }],
  };
  window.fetch = async () => new Response(
    JSON.stringify(CHANNELS[guild]),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  );

  const cfg = await import('/static/js/config-helpers.js');
  const first = await cfg.loadChannels();

  guild = 'B';                       // the operator picks the other server
  if (reset) cfg.resetMetaCaches();  // …which is what app.js's applyMeData does

  const second = await cfg.loadChannels();

  // What the panel would actually show, and save.
  const slot = document.querySelector('[data-slot]');
  const picker = cfg.mountChannelPicker(slot, second, '0', { label: 'Channel' });
  picker.getInput().focus();
  const labels = Array.from(document.querySelectorAll('.filter-select-item'))
    .map((el) => el.textContent.trim());

  return {
    first: first.map((c) => c.name),
    second: second.map((c) => c.name),
    labels,
  };
}
"""


def test_meta_caches_survive_within_a_guild(page):
    """Control: without a reset the memo is doing its job (one fetch, reused)."""
    out = page.evaluate(_GUILD_SWITCH, {"reset": False})
    assert out["first"] == ["alpha-general"]
    assert out["second"] == ["alpha-general"]


def test_guild_switch_reset_repopulates_the_picker(page):
    """After resetMetaCaches() the picker offers the NEW guild's channels.

    Before the fix the module globals outlived the switch, so every config
    panel listed the previous guild's channels/roles/members and a save wrote a
    foreign guild's snowflake into the new guild's config.
    """
    out = page.evaluate(_GUILD_SWITCH, {"reset": True})
    assert out["second"] == ["bravo-general"]
    assert any("bravo-general" in label for label in out["labels"]), out["labels"]
    assert not any("alpha-general" in label for label in out["labels"]), out["labels"]


# ── config-helpers.js: the shared member-picker option builder ───────────
#
# config-prune and config-inactive each carried a byte-identical private
# `memberOpts` (plus a private chip-name lookup). Hoisted to
# toSortedMemberOptions() / memberNameLookup(); these pin the behaviour both
# panels depend on so the shared version can't quietly change one of them.

# Deliberately scrambled input order, and the departed member sorts FIRST
# alphabetically — if `left` stopped leading the comparator the ghost would
# head the list, which is the failure that matters on an exemption picker.
_MEMBERS = [
    {"id": "700000000000000003", "name": "zoe", "display_name": "Zoe Zed",
     "left_server": False},
    {"id": "700000000000000001", "name": "aaron", "display_name": "",
     "left_server": True},
    # no display_name key at all — a member who never set a nickname
    {"id": "700000000000000002", "name": "beth", "left_server": False},
    # display name identical to the username: not worth printing twice
    {"id": "700000000000000004", "name": "carl", "display_name": "carl",
     "left_server": False},
]

_MEMBER_OPTS = """
async ({ members }) => {
  const cfg = await import('/static/js/config-helpers.js');
  return cfg.toSortedMemberOptions(members);
}
"""

_MEMBER_NAMES = """
async ({ members, ids }) => {
  const cfg = await import('/static/js/config-helpers.js');
  const lookup = cfg.memberNameLookup(members);
  return ids.map((id) => lookup(id));
}
"""


def test_sorted_member_options_put_departed_members_last(page):
    """Ordering, label formatting and the departure annotation in one pass.

    Sorting is on the bare label (localeCompare, so "Zoe" files with "zoe"
    rather than ahead of every lowercase name by code point); the
    "(left the server)" suffix is appended afterwards so it can't shift anyone.
    """
    opts = page.evaluate(_MEMBER_OPTS, {"members": _MEMBERS})
    assert [o["label"] for o in opts] == [
        "beth",
        "carl",
        "Zoe Zed (zoe)",
        "aaron (left the server)",
    ]
    assert [o["id"] for o in opts] == [
        "700000000000000002",
        "700000000000000004",
        "700000000000000003",
        "700000000000000001",
    ]
    assert [o["left"] for o in opts] == [False, False, False, True]


def test_sorted_member_options_keep_ids_as_strings(page):
    """Snowflakes past 2^53 lose digits as JS numbers, and the id is what the
    exemption PUT is addressed to."""
    opts = page.evaluate(
        _MEMBER_OPTS,
        {"members": [{"id": "700000000000000001", "name": "n", "left_server": False}]},
    )
    assert opts[0]["id"] == "700000000000000001"
    assert isinstance(opts[0]["id"], str)


def test_member_name_lookup_prefers_the_display_name(page):
    """Chip labels come from the member record, never from unpicking the
    picker's "Display (username)" label — a member called "Ana (EU)" would lose
    the bracket to that. An id nobody matches answers with the id itself."""
    out = page.evaluate(
        _MEMBER_NAMES,
        {
            "members": _MEMBERS + [
                {"id": "700000000000000005", "name": "ana",
                 "display_name": "Ana (EU)", "left_server": False},
            ],
            "ids": [
                "700000000000000003",  # display name wins
                "700000000000000001",  # blank display name → username
                "700000000000000002",  # no display_name key → username
                "700000000000000005",  # brackets survive verbatim
                "700000000000000009",  # departed since the config load
            ],
        },
    )
    assert out == ["Zoe Zed", "aaron", "beth", "Ana (EU)", "700000000000000009"]


# ── config-helpers.js + filter-select.js: the bounded member list ────────
#
# /api/meta/members returns a bounded page now (routes/meta.py): the payload
# used to carry every current member PLUS every departed known_users row, and
# grew forever with server churn. The security pass deliberately left it
# uncapped, because the pickers filter their cached copy CLIENT-side and a
# server cap would silently make everyone below it unselectable.
#
# What makes the bound safe is `opts.search` on the widget: the local filter
# stays instant, and in parallel the server is asked for the long tail. These
# run the same scenario with and without it — the "without" case is precisely
# the regression a naive cap would have shipped.

# A bounded first page (the live roster) and the tail behind it. Zephyr stands
# in for the departed member 5,000 rows down an alphabetical list.
_PICKER_ENV = """
  const PAGE = [
    { id: '700000000000000001', name: 'ana', display_name: 'Ana', left_server: false },
    { id: '700000000000000002', name: 'bo', display_name: 'Bo', left_server: false },
  ];
  const TAIL = [
    { id: '700000000000000009', name: 'zephyr', display_name: 'Zephyr Q',
      left_server: true },
  ];
  const calls = [];
  const respond = (body) => new Response(JSON.stringify(body),
    { status: 200, headers: { 'Content-Type': 'application/json' } });
  // Stands in for the endpoint: the bare path answers with the page only, and
  // ?q= / ?ids= reach the tail — exactly the contract routes/meta.py keeps.
  const serve = (url) => {
    calls.push(String(url));
    const u = new URL(String(url), location.origin);
    const q = (u.searchParams.get('q') || '').toLowerCase();
    const ids = u.searchParams.get('ids');
    if (ids) {
      const want = new Set(ids.split(','));
      return PAGE.concat(TAIL).filter((m) => want.has(m.id));
    }
    if (q) {
      return PAGE.concat(TAIL).filter(
        (m) => m.name.includes(q) || m.display_name.toLowerCase().includes(q));
    }
    return PAGE;
  };
"""

_PICKER_SEARCH = """
async ({ withSearch }) => {
  document.body.innerHTML = '<div id="host"><span data-slot></span></div>';
""" + _PICKER_ENV + """
  window.fetch = async (url) => respond(serve(url));

  const cfg = await import('/static/js/config-helpers.js');
  cfg.resetMetaCaches();
  const members = await cfg.loadMembers();
  const picker = cfg.mountMemberPicker(
    document.querySelector('[data-slot]'), members, '0',
    withSearch ? {} : { search: null },
  );

  const input = picker.getInput();
  input.focus();
  input.value = 'zephyr';
  input.dispatchEvent(new Event('input'));
  await new Promise((r) => setTimeout(r, 500));   // past the search debounce

  const items = Array.from(document.querySelectorAll('.filter-select-item'));
  const hit = items.find((el) => el.dataset.id === '700000000000000009');
  if (hit) hit.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));

  return {
    page: members.length,
    rows: items.map((el) => el.textContent.trim()),
    value: picker.getValue(),
    searched: calls.some((c) => c.includes('q=zephyr')),
  };
}
"""


def test_member_picker_without_search_cannot_reach_the_tail(page):
    """Control — and the bug a naive server cap would have caused.

    Local filtering alone can only ever offer what the bounded page contained,
    so the departed member is not merely hard to find: there is no sequence of
    keystrokes that selects them.
    """
    out = page.evaluate(_PICKER_SEARCH, {"withSearch": False})
    assert out["page"] == 2, "the stub is meant to serve a two-row page"
    assert not any("Zephyr" in row for row in out["rows"]), out["rows"]
    assert out["value"] == "0"     # nothing selectable, nothing selected
    assert not out["searched"]


def test_member_picker_reaches_a_member_outside_the_bounded_page(page):
    """THE load-bearing test: typing finds someone the page never shipped.

    Same picker, same two-row prefetch — the only difference is the server
    lookup the shared mount helper now wires in. The member arrives, is
    offered, and selects to their real snowflake.
    """
    out = page.evaluate(_PICKER_SEARCH, {"withSearch": True})
    assert out["page"] == 2
    assert out["searched"], "the widget never asked the server"
    assert any("Zephyr" in row for row in out["rows"]), out["rows"]
    # Departed members keep their annotation when they arrive by search.
    assert any("(left)" in row for row in out["rows"]), out["rows"]
    assert out["value"] == "700000000000000009"
    # Snowflakes past 2^53 lose digits as JS numbers, and the id is what the
    # save is addressed to.
    assert isinstance(out["value"], str)


_PICKER_SAVED_ID = """
async () => {
  document.body.innerHTML = '<div id="host"><span data-slot></span></div>';
""" + _PICKER_ENV + """
  window.fetch = async (url) => respond(serve(url));

  const cfg = await import('/static/js/config-helpers.js');
  cfg.resetMetaCaches();
  const members = await cfg.loadMembers();
  // A config that points at someone who left long ago — off the bounded page.
  const picker = cfg.mountMemberPicker(
    document.querySelector('[data-slot]'), members, '700000000000000009',
    { label: 'Community Host' },
  );
  const before = picker.getInput().value;
  await new Promise((r) => setTimeout(r, 300));
  return {
    before,
    after: picker.getInput().value,
    value: picker.getValue(),
    resolved: calls.some((c) => c.includes('ids=700000000000000009')),
  };
}
"""


def test_picker_resolves_a_saved_member_the_page_did_not_include(page):
    """A departed member a config references still renders as a person.

    The value is never in doubt — it is the id the config holds, and it stays a
    string throughout. What the lookup buys is the label: without it the field
    reads as a bare snowflake, which tells an admin nothing about whether the
    setting is still the one they meant.
    """
    out = page.evaluate(_PICKER_SAVED_ID)
    assert out["before"] == "700000000000000009"     # pre-lookup fallback
    assert out["resolved"], "the picker never asked to resolve the saved id"
    assert "Zephyr" in out["after"], out["after"]
    assert out["value"] == "700000000000000009"
    assert isinstance(out["value"], str)


_PICKER_UNMOUNT = """
async () => {
  document.body.innerHTML = '<div id="host"><span data-slot></span></div>';
""" + _PICKER_ENV + """
  // The search request hangs until the test lets it go, so the unmount is
  // guaranteed to happen while it is genuinely in flight.
  let release;
  const gate = new Promise((r) => { release = r; });
  window.fetch = async (url) => {
    const body = serve(url);
    if (String(url).includes('q=')) await gate;
    return respond(body);
  };

  const cfg = await import('/static/js/config-helpers.js');
  cfg.resetMetaCaches();
  const members = await cfg.loadMembers();
  const picker = cfg.mountMemberPicker(
    document.querySelector('[data-slot]'), members, '0');

  const input = picker.getInput();
  input.focus();
  input.value = 'zephyr';
  input.dispatchEvent(new Event('input'));
  await new Promise((r) => setTimeout(r, 300));   // debounce fired; fetch open

  document.getElementById('host').innerHTML = '';  // the panel unmounts
  picker.destroy();
  release();
  await new Promise((r) => setTimeout(r, 300));

  return {
    connected: picker.el.isConnected,
    rowsInDeadList: picker.el.querySelectorAll('.filter-select-item').length,
  };
}
"""


def test_search_result_landing_after_unmount_is_dropped(page):
    """A panel that navigates away mid-request must not be written into.

    The list holds one row at unmount — the "(none)" sentinel, since "zephyr"
    matched nothing in the local page. If the late reply were rendered anyway
    it would land as a second row in a detached tree, which is the leak class
    the review kept finding around debounced fetches.
    """
    out = page.evaluate(_PICKER_UNMOUNT)
    assert out["connected"] is False
    assert out["rowsInDeadList"] == 1, out
    assert not page.__dict__["_dk_errors"], page.__dict__["_dk_errors"]


# ── md-preview.js ────────────────────────────────────────────────────────

_MD = """
async ({ src }) => {
  document.body.innerHTML = '<div id="host"></div>';
  const mod = await import('/static/js/md-preview.js');
  document.getElementById('host').innerHTML = mod.mdInline(src);
  const a = document.querySelector('#host a');
  return a ? { href: a.getAttribute('href'), text: a.textContent } : null;
}
"""


def test_md_link_href_is_the_url_not_the_label(page):
    """`$1` is the bracket text; the validated URL is `$2`."""
    out = page.evaluate(
        _MD, {"src": "see [the rules](https://example.com/rules) first"}
    )
    assert out == {"href": "https://example.com/rules", "text": "the rules"}


def test_md_link_text_cannot_become_the_href(page):
    """The https-only guard is on the URL, so it only guards anything if the
    URL is what lands in the href.

    With the label used as the href, `[javascript:…](https://ok)` produced a
    real `javascript:` href — blocked only by an `onclick`, which a
    middle-click or "open in new tab" walks straight past.
    """
    out = page.evaluate(_MD, {"src": "[javascript:alert(1)](https://example.com/safe)"})
    assert out["href"] == "https://example.com/safe"
    assert not out["href"].lower().startswith("javascript:")


def test_md_link_rejects_a_non_http_scheme_outright(page):
    """No match, no anchor — the text is left as escaped plain markdown."""
    assert page.evaluate(_MD, {"src": "[click](javascript:alert(1))"}) is None
