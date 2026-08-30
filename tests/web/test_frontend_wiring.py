"""Source-level sweeps over the dashboard's JS wiring.

Cheap string checks, deliberately not a browser test: they catch a whole class
of "the two halves drifted" bug that only shows up when a user reloads or
switches servers, which no per-panel test exercises.

From the 2026-08-06 website deep review:

  * **todo hash id.** ``panels/todo.js`` mirrored its state into ``#/todo``
    while app.js registers the page as ``mod-todo``, so every render rewrote
    the URL to a route that does not exist and a reload or bookmark landed on
    "Page Not Available". The sweep generalises it: *every* hash a panel writes
    must name a route the router knows.
  * **S2 — guild-switch cache reset.** ``applyMeData`` is the one place that
    runs on boot and on every guild switch. If it stops clearing the
    guild-scoped module caches, config panels silently show the previous
    guild's channels/roles/members and saves write foreign snowflakes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_JS = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static" / "js"
_PANELS = _JS / "panels"


def _known_route_ids() -> set[str]:
    """Every id the hash router can mount: nav pages, extra routes, help pages."""
    ids = set(re.findall(r'\bid:\s*"([A-Za-z0-9_-]+)"', (_JS / "app.js").read_text(encoding="utf-8")))
    # Help pages are generated from help-sections.js rather than listed in app.js.
    ids |= set(
        re.findall(
            r'page:\s*"([A-Za-z0-9_-]+)"',
            (_PANELS / "help-sections.js").read_text(encoding="utf-8"),
        )
    )
    return ids


def _panel_files() -> list[Path]:
    return sorted(_PANELS.glob("*.js"))


@pytest.fixture(scope="module")
def route_ids() -> set[str]:
    ids = _known_route_ids()
    # Sanity: the registry parse must not silently come back empty, or every
    # assertion below passes for the wrong reason.
    assert "mod-todo" in ids and len(ids) > 50, "route-id scrape looks broken"
    return ids


def test_every_synced_hash_names_a_real_route(route_ids):
    """`syncHash(id, …)` writes the URL a reload will be asked to restore."""
    bad = [
        (path.name, panel_id)
        for path in _panel_files()
        for panel_id in re.findall(r'syncHash\(\s*"([^"]+)"', path.read_text(encoding="utf-8"))
        if panel_id not in route_ids
    ]
    assert not bad, (
        "panels sync the URL to routes the router doesn't know — reload/bookmark "
        f"lands on 'Page Not Available': {bad}"
    )


def test_every_hand_written_hash_names_a_real_route(route_ids):
    """The same check for panels that call history.replaceState directly.

    Only literal ids are checked; a templated `#/${to}` is skipped because its
    target isn't knowable from the source.
    """
    bad = [
        (path.name, panel_id)
        for path in _panel_files()
        for panel_id in re.findall(
            r"""replaceState\([^;]*?["'`]\#/([A-Za-z0-9_-]+)""",
            path.read_text(encoding="utf-8"),
            re.S,
        )
        if panel_id not in route_ids
    ]
    assert not bad, f"panels rewrite the URL to unknown routes: {bad}"


def test_every_rendered_link_names_a_real_route(route_ids):
    """`href="#/id"` links inside rendered panel markup, and in manual.html.

    The two tests above cover the URL a panel *writes*. They do not cover the
    links a panel *draws*, which is most of the cross-referencing on the
    dashboard — every "set that on <a href=#/economy-config>Settings</a>" hint.
    A typo there produces a link that looks fine, renders fine, and lands on
    "Page Not Available"; nothing failed when the Spending section was split
    three ways and every one of those hints had to be repointed.

    A retired id is legitimate if MOVED_PAGES redirects it, so those count as
    known routes too.
    """
    app = (_JS / "app.js").read_text(encoding="utf-8")
    moved = set(re.findall(r'^\s*"([A-Za-z0-9_-]+)":\s*"[A-Za-z0-9_-]+",', app, re.M))
    known = route_ids | moved

    targets: list[tuple[str, str]] = []
    sources = [*_panel_files(), _JS.parent / "manual.html"]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        # Both forms occur: "#/id" inside panels, "/#/id" in the standalone manual.
        for hit in re.findall(r'href="/?#/([A-Za-z0-9_-]+)', text):
            targets.append((path.name, hit))

    assert targets, "link scrape found nothing — the pattern must have drifted"
    bad = sorted({t for t in targets if t[1] not in known})
    assert not bad, (
        "rendered links point at routes the router cannot mount — they render "
        f"normally and land on 'Page Not Available': {bad}"
    )


def test_apply_me_data_drops_guild_scoped_caches():
    """S2: the guild switch must invalidate every guild-scoped module cache.

    `/api/config` and every `/api/meta/*` list is scoped to the active guild,
    but config-helpers.js memoizes them in module globals and switchGuild
    re-mounts panels without reloading the page.
    """
    app = (_JS / "app.js").read_text(encoding="utf-8")
    body = app.split("function applyMeData(", 1)[1].split("\nfunction ", 1)[0]
    assert "resetMetaCaches()" in body, (
        "applyMeData no longer clears config-helpers' meta caches — config "
        "panels will show the previous guild's channels/roles/members"
    )
    assert "_resetPanelSpecCache()" in body
    assert 'from "./config-helpers.js"' in app


def test_reset_meta_caches_clears_every_cache_it_owns():
    """A cache added to config-helpers.js must be cleared on guild switch too.

    Two declaration shapes now: the `let _x = null` list memos, and the Map
    memos the bounded member list brought (search results and the id index).
    Both hold guild-scoped rows, so both have to be dropped on a switch.
    """
    src = (_JS / "config-helpers.js").read_text(encoding="utf-8")
    reset = src.split("export function resetMetaCaches()", 1)[1].split("\n}", 1)[0]

    nulled = set(re.findall(r"^let (_[A-Za-z]+) = null;", src, re.M))
    maps = set(re.findall(r"^const (_[A-Za-z]+) = new (?:Map|Set)\(\);", src, re.M))
    assert nulled and maps, "cache-declaration scrape looks broken"

    missing = {n for n in nulled if f"{n} = null" not in reset}
    missing |= {n for n in maps if f"{n}.clear()" not in reset}
    assert not missing, (
        f"guild-scoped caches not cleared by resetMetaCaches(): {sorted(missing)}"
    )


# ── the bounded member list (2026-08 review residue) ───────────────────────
#
# /api/meta/members is paginated now. A picker that only filters the page it was
# handed makes everyone below the cut unselectable — the exact regression that
# kept the endpoint uncapped through the security pass. These two keep the
# server-side lookup wired wherever member options are built.


def test_shared_member_pickers_wire_the_server_search():
    """The two mount helpers most panels go through carry it by default."""
    src = (_JS / "config-helpers.js").read_text(encoding="utf-8")
    for fn in ("mountMemberPicker", "mountMemberMultiPicker"):
        body = src.split(f"export function {fn}(", 1)[1].split("\n}", 1)[0]
        assert "search: memberSearch()" in body, (
            f"{fn} no longer passes the server-side member lookup — every "
            "member outside the bounded first page just became unselectable"
        )
        assert "_hydrateSavedMembers" in body, (
            f"{fn} no longer resolves a saved id the page didn't include; a "
            "config naming a departed member renders as a bare snowflake"
        )
    # …and the widget has to honour it.
    fs = (_JS / "filter-select.js").read_text(encoding="utf-8")
    assert "opts.search" in fs, "filter-select dropped remote search support"


_MEMBER_OPTION_BUILDERS = ("toMemberOptions(", "toSortedMemberOptions(")


def test_panels_building_member_options_wire_the_server_search():
    """A panel assembling member options by hand owns the lookup itself.

    Going through mountMemberPicker/mountMemberMultiPicker is the easy path and
    needs nothing; a panel that reaches for the option builder directly (because
    it wants a different ordering, say) has opted out of that wiring and has to
    pass `search:` itself.
    """
    offenders = []
    for path in _panel_files():
        src = path.read_text(encoding="utf-8")
        if not any(b in src for b in _MEMBER_OPTION_BUILDERS):
            continue
        if "memberSearch(" not in src:
            offenders.append(path.name)
    assert not offenders, (
        "panels building member picker options without a server-side search: "
        f"{offenders} — /api/meta/members is a bounded page, so anyone below "
        "the cut can no longer be picked"
    )


# ── IA1 / IA3 (2026-08 information-architecture pass) ───────────────────────


# Route ids are frozen: deep links, bookmarks, the 47 `help:` mappings and the
# never-opened telemetry list all key off them, so the Games regroup moved nav
# *entries*, never ids. These are the ones the regroup touched.
_REGROUPED_ROUTE_IDS = [
    # Games → Operations
    "games-logs", "games-scheduling", "games-config", "games-external",
    # Games → Live Games (photo-challenge folded in from its own section)
    "games-legitlibs", "config-risky-rolls", "config-games-pressure",
    "config-games-quickdraw", "config-games-hotpotato",
    "config-games-hotpotatogroup", "config-games-chicken",
    "config-games-musicalchairs", "photo-challenge",
    # ...and games-ama, moved here from Question Banks in 2026-08 when its bank
    # UI came out: AMA questions come from members mid-round, never the bank.
    "games-ama",
    # Games → Question Banks
    "games-wyr", "games-nhie", "games-mlt", "games-rushmore", "games-price",
    "games-clapback", "games-ffa", "games-traditional",
    # → the new Social section
    "config-guess", "config-whisper", "pen-pals", "config-confessions",
]


def test_regrouped_routes_kept_their_ids(route_ids):
    missing = [pid for pid in _REGROUPED_ROUTE_IDS if pid not in route_ids]
    assert not missing, (
        "the IA regroup was supposed to move nav entries, not rename routes — "
        f"these ids no longer exist: {missing}"
    )


# The mod workflow panels (IA3). The browser suite proves the round trip; this
# is the cheap always-on guard that the wiring is still there at all.
_URL_STATE_PANELS = {
    "mod-tickets.js": "mod-tickets",
    "mod-jails.js": "mod-jails",
    "rules-watch.js": "rules-watch",
    "qa-tracker.js": "qa-tracker",
    "todo.js": "mod-todo",
}


@pytest.mark.parametrize("filename,page_id", sorted(_URL_STATE_PANELS.items()))
def test_mod_workflow_panels_keep_url_state(filename, page_id):
    """Each mod queue reads its state from the hash and writes it back."""
    src = (_PANELS / filename).read_text(encoding="utf-8")
    assert "initialParams" in src, f"{filename} ignores the hash params on mount"
    assert f'syncHash("{page_id}"' in src, f"{filename} stopped mirroring state into the URL"


# ── table.js column contract (S1 follow-through) ────────────────────────────
#
# table.js escapes every cell by default; a column that deliberately renders
# markup opts in with `html: true` and then owns its own escaping. Two ways for
# a panel to get that wrong, and both are invisible until someone looks at the
# page:
#
#   * a markup-returning column WITHOUT `html: true` prints literal `<span…>`
#     text at the reader;
#   * a plain-text column that still calls esc() double-escapes, so "Tom &
#     Jerry" renders as "Tom &amp; Jerry".
#
# The allow-list below is the point of the second test: adding a new markup
# column is a deliberate act that has to be justified here, which is the review
# gate that keeps a member-controlled name from ever getting `html: true`.


def _column_specs() -> list[tuple[str, str]]:
    """Every column object literal in every renderSortableTable consumer.

    Returns (filename, source-of-the-object) pairs. Brace-matched rather than
    line-based because a format() body can span several lines.
    """
    specs: list[tuple[str, str]] = []
    for path in _panel_files():
        src = path.read_text(encoding="utf-8")
        if "renderSortableTable" not in src:
            continue
        for match in re.finditer(r"\{\s*key:\s*\"", src):
            depth = 0
            start = match.start()
            for i in range(start, len(src)):
                if src[i] == "{":
                    depth += 1
                elif src[i] == "}":
                    depth -= 1
                    if depth == 0:
                        specs.append((path.name, src[start : i + 1]))
                        break
    return specs


def test_column_specs_scrape_finds_the_known_consumers():
    """Guard the guard: an empty or tiny scrape would pass both tests below."""
    files = {name for name, _ in _column_specs()}
    assert "channels.js" in files and "usage-telemetry.js" in files
    assert len(_column_specs()) > 40, "column scrape looks broken"


def test_no_text_column_escapes_its_own_output():
    """table.js escapes cells now — a leftover esc() double-escapes."""
    bad = [
        (name, spec.split("\n")[0][:70])
        for name, spec in _column_specs()
        if "esc(" in spec and "html: true" not in spec
    ]
    assert not bad, (
        "these columns escape text that table.js already escapes, so names with "
        f"& < > render mangled: {bad}"
    )


# (file, key) of every column that opts out of escaping, with why it is safe.
# Adding a row here is a security decision: the column then owns its escaping,
# so it must interpolate computed values (numbers, fixed class names) only —
# never a member-supplied name.
_HTML_COLUMNS = {
    # colored score/percentage/± figures: the value is a number, the color a literal
    ("quality-score.js", "final_score"),
    ("retention.js", "drop_pct"),
    ("retention.js", "normalized_drop_pct"),
    ("xp-leaderboard.js", "_diff"),
    ("channels.js", "avg_sentiment"),
    ("channels.js", "trend_pct"),
    # <code>-wrapped command / panel identifier; CODE() esc()s the value itself
    ("usage-telemetry.js", "name"),
    # <span class="num-err|num-dim">: a Number()-coerced count
    ("usage-telemetry.js", "errors"),
    # anchor built by sourceCell(): href/host both esc()'d, non-http(s) falls back to esc(url)
    ("music-playlist.js", "source_url"),
    # reason badge from reasonCell(): class name is a literal, label esc()'d
    ("music-playlist.js", "removal_reason"),
    # action buttons keyed by a server-issued row id, Number()-coerced into data-* attrs
    ("music-playlist.js", "id"),
    # Set Active button keyed by the card's DB row id, Number()-coerced into data-activate
    ("mahjong.js", "row_id"),
}


def test_every_html_opt_in_column_is_on_the_reviewed_list():
    """A new `html: true` column must be added here — deliberately, with a
    reason — rather than slipping in beside a display name."""
    found = {
        (name, re.search(r'key:\s*"([^"]+)"', spec).group(1))
        for name, spec in _column_specs()
        if "html: true" in spec
    }
    unreviewed = found - _HTML_COLUMNS
    assert not unreviewed, (
        "columns opted out of table.js escaping without review — if any of these "
        f"renders member-supplied text it is stored XSS: {sorted(unreviewed)}"
    )
    assert not _HTML_COLUMNS - found, (
        f"reviewed html columns that no longer exist: {sorted(_HTML_COLUMNS - found)}"
    )


def test_member_name_columns_never_opt_out_of_escaping():
    """The six columns S1 was actually about: raw Discord display names."""
    member_keys = {
        "user_name", "display_name", "greeter_name", "pair_name", "channel_name",
    }
    bad = [
        (name, key)
        for name, key in {
            (n, re.search(r'key:\s*"([^"]+)"', s).group(1))
            for n, s in _column_specs()
            if "html: true" in s
        }
        if key in member_keys
    ]
    assert not bad, f"member-controlled column rendered as HTML: {bad}"


# ── F1: no panel may mount a loader it never catches ───────────────────────


# Panels whose bare `(async () => {…})()` is genuinely safe: the whole body is
# inside its own try/catch that renders a failure state, or the awaited calls
# cannot reject. Everything else must go through mountAsync so a failed first
# fetch is an error state with a retry, not a spinner that never clears.
_IIFE_EXEMPT = {
    # secondary loads inside an already-mounted panel, each with its own catch
    "config-spoiler.js",   # the "recent activity" box (try/catch → .empty)
    "config-ai.js",        # model-status poller
    "message-search.js",   # top-level try
    "grant-audit.js",      # try/catch → statusEl error text
    "inactive-report.js",  # loadChannels/loadRoles never reject; refresh() catches
    "nsfw-tags-report.js", # try/catch → .error, plus a cancelled flag
    "nsfw-gender.js",      # loadChannels/refresh both catch
    "activity.js",         # loadDropdowns/refresh both catch
    "config-bump-tracker.js",  # try/catch → renderError
    "todo.js",             # documented in-file; owned by the mod-queue work
    "qa-tracker.js",
}


def test_no_panel_mounts_an_uncaught_async_loader():
    """F1 was ~27 panels deep; this stops the 28th.

    `(async () => { … })()` with no `.catch` turns one failed fetch into a
    permanent "Loading…" plus an unhandled rejection, with nothing on screen to
    tell the user which it is.
    """
    offenders = []
    for path in _panel_files():
        if path.name in _IIFE_EXEMPT:
            continue
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\(async \(\) => \{", src):
            tail_start = src.find("})()", match.end())
            if tail_start == -1:
                continue
            if ".catch(" in src[tail_start : tail_start + 12]:
                continue
            offenders.append((path.name, src[: match.start()].count("\n") + 1))
    assert not offenders, (
        "async loaders with no rejection handling — a failed fetch leaves the "
        f"panel on 'Loading…' forever: {offenders}"
    )


def test_the_iife_exemption_list_has_no_dead_entries():
    """An exempted panel that no longer has the pattern should leave the list,
    or the exemption silently covers a future regression in that file."""
    stale = [
        name
        for name in _IIFE_EXEMPT
        if "(async () => {" not in (_PANELS / name).read_text(encoding="utf-8")
    ]
    assert not stale, f"exempt panels with no async IIFE left: {sorted(stale)}"


def test_converted_panels_pass_a_specific_error_message():
    """A generic "Couldn't load this page." tells the reader nothing about
    which of the ~130 panels failed or what to retry."""
    generic = []
    for path in _panel_files():
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"errorMsg:\s*\"([^\"]*)\"", src):
            msg = match.group(1)
            if msg in {"", "Couldn't load this page.", "Couldn’t load this page."}:
                generic.append((path.name, msg))
    assert not generic, f"panels with a non-specific mountAsync message: {generic}"


# ── the shared member-picker option builder ────────────────────────────────
#
# config-prune and config-inactive each carried a byte-identical private
# `memberOpts` (departed members sorted last, labelled "(left the server)") and
# a private `nameById`/`memberName` pair. Both now live in config-helpers.js as
# toSortedMemberOptions() and memberNameLookup(). The review found several
# helper pairs that had drifted silently once copied, so these two checks keep
# the shape in exactly one place rather than trusting nobody re-inlines it.


_SHARED_MEMBER_HELPER_PANELS = ("config-prune.js", "config-inactive.js")


@pytest.mark.parametrize("filename", _SHARED_MEMBER_HELPER_PANELS)
def test_member_picker_panels_use_the_shared_helpers(filename):
    """Neither panel may re-declare the private copies that were hoisted."""
    src = (_PANELS / filename).read_text(encoding="utf-8")
    assert "toSortedMemberOptions" in src, (
        f"{filename} no longer imports the shared member-option builder"
    )
    assert "memberNameLookup" in src, (
        f"{filename} no longer imports the shared chip-name lookup"
    )
    # Binding the shared call to a local `memberOpts` is fine; building one is
    # not. A `nameById` Map is the private half of memberNameLookup and has no
    # business back in a panel at all.
    rebuilt = [
        line.strip()[:70]
        for line in src.splitlines()
        if re.match(r"\s*(?:const|let)\s+(?:memberOpts|nameById)\s*=", line)
        and "toSortedMemberOptions(" not in line
    ]
    assert not rebuilt, (
        f"{filename} builds its own member options again: {rebuilt} — the "
        "private copy this pair was hoisted out of is back, and the two panels "
        "can drift apart again"
    )


def test_only_config_helpers_builds_the_departed_member_label():
    """The wording is the tell: a panel spelling it out inline is a fresh copy.

    (activity.js / message-search.js use the *terse* " (left)" from
    toMemberOptions, which is a different, intentional label — not this one.)
    """
    offenders = [
        path.name
        for path in _panel_files()
        if "(left the server)" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "panels rebuilding the sorted member-picker options inline instead of "
        f"calling toSortedMemberOptions(): {offenders}"
    )
    helpers = (_JS / "config-helpers.js").read_text(encoding="utf-8")
    assert "(left the server)" in helpers, "the shared builder lost its label"


def test_mount_async_conversion_covered_the_config_family():
    """The F1 sweep's headline set — if one of these regresses to a bare IIFE
    the test above catches it, but this names the contract directly."""
    missing = [
        path.name
        for path in _panel_files()
        if path.name.startswith("config-")
        and path.name not in {"config-bios.js"}
        and "(async () => {" not in path.read_text(encoding="utf-8")
        and "mountAsync" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"config panels with neither a loader nor mountAsync: {missing}"
