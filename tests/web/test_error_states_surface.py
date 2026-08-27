"""Failure states that lied: three shapes where an error never reached the user.

Each is the same class of bug — a rejection handled somewhere that produces a
worse outcome than not handling it at all.

  * **The refresh that keeps the old numbers.** Seven health panels defined
    ``function reload() { return load().then(decorate); }`` and guarded only
    the *first* call. The Show Bots toggle called ``reload()`` bare, and
    ``load()`` rejects before it touches innerHTML — so a failed refetch left
    the previous figures on screen with the checkbox already flipped and
    nothing said. Reading bot-excluded numbers under a ticked "include bots" is
    worse than an error, because it looks like an answer.

  * **The retry that could never run.** Five panels wrapped their mountAsync
    loader's own fetch in a try/catch that rendered a bare error and returned
    *normally*, so the rejection never reached mountAsync. Its ``renderFailure``
    draws the error plus a working "Try again" button, and the ``errorMsg``
    those panels carefully declare was dead code.

  * **The unsaved-edits warning that disarms itself.** ``_dirty`` was one
    module-global boolean cleared by any successful ``showStatus``. Fourteen
    panels guard two to four forms, so saving one form silently dropped the
    warning protecting half-typed values in all the others.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_JS = Path("src/web_server/static/js")
_PANELS = _JS / "panels"

# The seven that shared the copied reload block, byte for byte.
RELOADABLE = [
    "health-composite-score.js", "health-heatmap.js", "health-dau-mau.js",
    "health-sentiment.js", "health-cohort-retention.js", "health-gini.js",
    "health-newcomer-funnel.js",
]

# The five whose inner catch defeated mountAsync's retry. wellness-caps.js is
# deliberately absent: it rethrows on first load and handles later refreshes in
# place, which is the correct shape and the one the others now follow.
RETHROWERS = [
    "wellness-home.js", "wellness-away.js", "wellness-history.js",
    "wellness-admin.js", "games-external.js",
]


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("panel", RELOADABLE)
def test_health_panels_guard_every_refresh_not_just_the_first(panel: str) -> None:
    src = _src(_PANELS / panel)
    assert "mountReloadable(container" in src, f"{panel}: not using the shared helper"
    assert not re.search(r"^\s*reload\(\)\.catch\(", src, re.M), (
        f"{panel}: still guards only the initial load"
    )
    assert not re.search(r"function reload\(\) \{\n\s*return load\(\)\.then\(decorate\);", src), (
        f"{panel}: the unguarded reload is back"
    )


def test_the_shared_reloader_catches_on_every_pass() -> None:
    """Guard the guard: seven panels delegate their whole failure story here."""
    src = _src(_JS / "report-helpers.js")
    assert "export function mountReloadable" in src
    body = src.split("export function mountReloadable", 1)[1].split("\nexport ", 1)[0]
    assert ".then(decorate).catch(" in body, "the catch is not on the reload path"
    assert body.count("function reload()") == 1
    assert "return reload;" in body, "panels need the handle for their own controls"


@pytest.mark.parametrize("panel", RETHROWERS)
def test_mount_loaders_let_their_rejection_reach_mount_async(panel: str) -> None:
    src = _src(_PANELS / panel)
    # The loader body is everything up to the end of the mountAsync callback's
    # first statement block; catching the panel's own top-level fetch and
    # writing into .panel is the shape that defeated the retry.
    assert not re.search(
        r'\} catch \([^)]*\) \{\s*\n\s*container\.querySelector\("\.panel"\)\.innerHTML =?\s*\n?\s*renderError\(',
        src,
    ), f"{panel}: the loader still swallows its own rejection"


def test_wellness_caps_keeps_its_deliberate_in_place_refresh() -> None:
    """It rethrows on first load and renders in place afterwards. That is
    correct and must not be "fixed" into the others' shape."""
    src = _src(_PANELS / "wellness-caps.js")
    assert "if (firstLoad) throw e;" in src


def test_the_dirty_flag_is_tracked_per_form() -> None:
    src = _src(_JS / "config-helpers.js")
    assert "const _dirtyForms = new Set()" in src, "still one page-wide boolean"
    assert "_dirtyForms.add(form)" in src
    assert 'el.closest?.("[data-dk-guard]")' in src, (
        "showStatus must attribute the save to the form it came from"
    )
    assert 'form.dataset.dkGuard = "1"' in src, "guarded containers must be findable"
    assert not re.search(r"^let _dirty = false;", src, re.M)


def test_a_guild_switch_drops_the_tracked_forms() -> None:
    """The set holds form *elements*, and a guild switch rebuilds every panel.
    Left alone it would retain detached nodes and warn about unsaved edits on
    forms that no longer exist."""
    src = _src(_JS / "config-helpers.js")
    reset = src.split("export function resetMetaCaches()", 1)[1].split("\n}", 1)[0]
    assert "_dirtyForms.clear()" in reset


# ── optimistic writes that outlive their own failure ────────────────────


def test_a_failed_separation_write_is_rolled_back() -> None:
    """Pen Pals mutates the separations list and re-renders before the PUT, so
    the row appears at once. `persistSeps` reported a failure and left it
    there — and this is the keep-them-apart list, so a separation on screen
    that the server never stored is the one failure this page must not have."""
    src = _src(_PANELS / "pen-pals-settings.js")
    assert "const persistSeps = async (undo)" in src, "no rollback is passed in"
    assert "undo();" in src and "renderSeps();" in src
    # Both mutation sites have to supply one, or the rollback is decorative.
    assert src.count("await persistSeps(() =>") == 2, (
        "a caller mutates the list without supplying its undo"
    )
    assert "await persistSeps();" not in src


def test_a_failed_order_fetch_does_not_eat_the_empty_state() -> None:
    """The failure message was written into the empty-state element itself, so
    once a fetch failed, every later successful fetch with nothing waiting
    still read "Couldn't load the orders."."""
    src = _src(_PANELS / "shop-approvals.js")
    assert 'emptyEl.textContent = "Couldn’t load the orders."' not in src
    assert "listEl.innerHTML = renderError(" in src


def test_mahjong_holds_its_rebuild_while_house_rules_is_dirty() -> None:
    """A card upload or Set Active remounts the whole panel, which throws away
    anything half-typed in House Rules."""
    src = _src(_PANELS / "mahjong.js")
    assert "isFormDirty(form)" in src, "the rebuild still runs unconditionally"
    assert "isFormDirty," in src, "isFormDirty is used but not imported"


def test_mahjong_does_not_report_that_through_show_status() -> None:
    """[data-status] lives inside [data-form], and showStatus(el, true, …)
    clears the dirty flag for whichever guarded form its element sits in.
    Reporting the held rebuild through it would disarm the very state that
    produced the message, so the next upload would remount and take the edits
    with it. This is a real interaction between two fixes, not a style point."""
    src = _src(_PANELS / "mahjong.js")
    guard = src.split("function remount()", 1)[1].split("\n    }", 1)[0]
    # The comment explaining the choice names showStatus, so read code only.
    code = "\n".join(
        ln for ln in guard.splitlines() if not ln.lstrip().startswith("//")
    )
    assert "toast(" in code, "the held-rebuild notice must not go through showStatus"
    assert "showStatus(" not in code


def test_is_form_dirty_reads_the_same_registry_guard_form_writes() -> None:
    src = _src(_JS / "config-helpers.js")
    assert "export function isFormDirty(form)" in src
    assert "return _dirtyForms.has(form);" in src


# ── controls that changed state without telling anyone ──────────────────


def test_wellness_caps_guards_its_primary_control() -> None:
    """guardForm was attached only to the Add Manual Cap form, inside a
    collapsed <details> at the bottom. The panel's actual control — 24 sliders
    and a drag-a-point-on-the-chart interaction — sat outside every guarded
    container, so setting every cap and navigating away discarded the lot with
    no prompt."""
    src = _src(_PANELS / "wellness-caps.js")
    assert "data-histo-form" in src, "the histogram is not wrapped"
    assert 'guardForm(container.querySelector("[data-histo-form]"))' in src


def test_wellness_caps_drops_the_guarded_node_before_destroying_it() -> None:
    """load() rewrites the histogram on every mode or lookback change. Without
    this the registry keeps a detached element and reports unsaved edits on a
    form that is no longer in the document."""
    src = _src(_PANELS / "wellness-caps.js")
    assert "clearFormDirty(histoForm)" in src
    rebuild = src.index('container.querySelector(".panel").innerHTML = `')
    assert src.index("clearFormDirty(histoForm)") < rebuild, (
        "the node is forgotten after it is destroyed, which is too late"
    )


def test_a_canvas_drag_marks_the_form_dirty() -> None:
    """Dragging a cap point fires neither input nor change, so the most direct
    way to set a cap was invisible to the guard. dk:change is the repo's own
    answer — filter-select already dispatches it for the same reason."""
    src = _src(_PANELS / "wellness-caps.js")
    assert 'new CustomEvent("dk:change", { bubbles: true })' in src
    # Every way a drag can end, or the guard is armed only sometimes.
    for ending in ("mouseup", "mouseleave", "touchend"):
        block = src.split(f'canvas.addEventListener("{ending}"', 1)[1].split("});", 1)[0]
        assert "markDirty()" in block, f"a drag ending on {ending} leaves no trace"


def test_config_roles_announces_a_permission_change() -> None:
    """Add and remove are button clicks that mutate a JS array, and a click
    fires nothing guardForm listens for. Remove was untracked outright; add was
    tracked only by accident, because the picker beside it happens to dispatch
    dk:change — so the bug looked half-present."""
    src = _src(_PANELS / "config-roles.js")
    block = src.split("function refreshPermList", 1)[1].split("\n  }", 1)[0]
    assert 'dispatchEvent(new CustomEvent("dk:change"' in block, (
        "the permission list still changes silently"
    )


def test_the_quote_border_panel_does_not_invent_an_empty_state() -> None:
    """_quote_border_meta returns 200 with exists:false when no border is set,
    so a rejection is always a real failure. Swallowing it rendered "No custom
    border yet — quote cards use the bundled Golden Poppy frame", a confident
    claim about configuration that a 500 or a timeout makes false."""
    src = _src(_PANELS / "config-quote-border.js")
    assert "fall through to the empty state" not in src
    assert 'const meta = await api("/api/config/quote-border");' in src


def test_the_playlist_bulk_delete_uses_the_house_dialog() -> None:
    """It is the most destructive control on the page — an irreversible write
    to a real Spotify playlist, including songs the bot never added."""
    src = _src(_PANELS / "music-playlist.js")
    assert "window.confirm" not in src
    assert "await confirmDialog(" in src
    # Cancelling is not a no-op: the additions already ran. Reporting success
    # would also clear every guarded form on the page.
    cancel = src.split("if (!ok) {", 1)[1].split("}", 1)[0]
    assert "toast(" in cancel and "showStatus" not in cancel


def test_the_playlist_maintenance_card_is_guarded() -> None:
    """Its status element sits outside every other guarded container, so a
    successful Re-scan fell back to the page-wide clear and disarmed the
    unsaved-edits warning on the settings form above it."""
    src = _src(_PANELS / "music-playlist.js")
    assert "guardForm(cardMaint)" in src


def test_intake_report_does_not_send_admins_to_a_page_that_does_not_exist() -> None:
    """It said "enable them under Config → Intake Cards". There is no such
    page: app.js has one intake route, and the switch is on this same page."""
    src = _src(_PANELS / "intake-report.js")
    assert "Config → Intake Cards" not in src
    assert "Card Settings" in src
