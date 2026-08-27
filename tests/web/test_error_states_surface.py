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
