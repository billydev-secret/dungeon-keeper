"""Contracts a panel silently breaks: CSS classes, action vocabularies, and
values interpolated into HTML attributes.

Each of these fails without an error. A class that does not exist styles
nothing; an action key the bot never writes filters to zero rows; an
unescaped value only matters when someone puts a quote in it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "src" / "web_server" / "static"
_PANELS = _STATIC / "js" / "panels"
_SRC = _ROOT / "src"


# ── the button kit is a closed vocabulary ───────────────────────────────


def _declared_classes() -> set[str]:
    """Every class a stylesheet defines — including the <style> blocks some
    panels inject, which are as real as app.css and are where .rw-back lives."""
    names: set[str] = set()
    sources = [p.read_text(encoding="utf-8") for p in _STATIC.rglob("*.css")]
    for panel in _PANELS.parent.rglob("*.js"):
        src = panel.read_text(encoding="utf-8")
        sources += re.findall(r"<style[^>]*>(.*?)</style>", src, re.S)
    for css in sources:
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        names |= set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", css))
    return names


def _js_hook_classes() -> set[str]:
    """Classes used as querySelector/closest targets. These are behaviour
    hooks that deliberately carry no styling, so an absent CSS rule is not a
    bug — .vt-dl-btn, .msg-ctx-more and .rw-back are all of this kind."""
    names: set[str] = set()
    for panel in _PANELS.parent.rglob("*.js"):
        src = panel.read_text(encoding="utf-8")
        for m in re.finditer(r'(?:querySelector(?:All)?|closest|matches)\(\s*["\'`]([^"\'`]+)', src):
            names |= set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", m.group(1)))
    return names


def test_every_btn_modifier_names_a_class_that_exists():
    """chat-revive shipped `class="btn small"`, `btn small danger` and
    `btn primary` — ten controls, including a delete, wearing modifiers the
    stylesheet has never defined. The kit is .btn plus .btn-primary /
    .btn-danger / .btn-ghost / .btn-sm, so those rendered as plain buttons."""
    known = _declared_classes() | _js_hook_classes()
    offenders = []
    for panel in sorted(_PANELS.glob("*.js")):
        src = panel.read_text(encoding="utf-8")
        for m in re.finditer(r'class="((?:btn|act-btn)[^"$]*)"', src):
            for cls in m.group(1).split():
                if cls not in known:
                    ln = src[: m.start()].count("\n") + 1
                    offenders.append(f"{panel.name}:{ln}: .{cls} in class=\"{m.group(1)}\"")
    assert not offenders, (
        "button classes that no stylesheet defines:\n" + "\n".join(offenders)
    )


# ── the audit panel's action vocabulary is the bot's ────────────────────


def test_mod_audit_action_keys_are_strings_the_bot_actually_writes():
    """An action the bot never writes gives a filter option that matches zero
    rows and a label lookup that misses, so the row renders its raw key. Six of
    the twelve keys were short forms nobody wrote — `jail` for `jail_create`,
    `warn` for `warning_issue`, `pull` for `channel_pull` — over a live log
    holding 10 jails, 8 releases, 6 warnings and 9 pulls.

    The comparison is against `action="..."` keyword arguments specifically,
    not against every quoted token in the tree: `"jail"` appears in the bot for
    plenty of unrelated reasons, and a looser scrape passes this test with the
    original bug still in place.
    """
    src = (_PANELS / "mod-audit.js").read_text(encoding="utf-8")
    block = src.split("const ACTION_LABELS = {", 1)[1].split("};", 1)[0]
    keys = set(re.findall(r"^\s*([a-z_]+)\s*:", block, re.M))
    assert keys, "could not read ACTION_LABELS"

    written: set[str] = set()
    for py in list((_SRC / "bot_modules").rglob("*.py")) + list((_SRC / "web_server").rglob("*.py")):
        written |= set(re.findall(r'action="([a-z_.]+)"', py.read_text(encoding="utf-8")))
    assert "jail_create" in written, "the action= scrape looks broken"

    missing = sorted(k for k in keys if k not in written)
    assert not missing, (
        "mod-audit action keys the bot never writes, so their filter option "
        f"can never match a row: {missing}"
    )


# ── nothing untrusted reaches an attribute unescaped ────────────────────


HASH_PANELS = ["quality-score.js", "invite-effectiveness.js", "retention.js",
               "grant-audit.js"]


@pytest.mark.parametrize("panel", HASH_PANELS)
def test_url_params_are_coerced_before_reaching_an_attribute(panel: str):
    """`mount(el, params)` is handed the parsed window.location.hash, so these
    are attacker-controlled through a link. They are all number inputs, so
    parseInt is both the fix and the correct read of the value."""
    src = (_PANELS / panel).read_text(encoding="utf-8")
    raw = re.findall(r'value="\$\{\s*(initialParams\.[A-Za-z_]+)[^}]*\}"', src)
    assert not raw, (
        f"{panel}: URL params reach a value attribute unescaped: {raw}"
    )


FREE_TEXT = {
    "economy-config.js": ["currency_name", "currency_plural", "currency_emoji",
                          "wallet_name", "currency_icon_url"],
    "xp-settings.js": ["cooldown_thresholds_seconds", "cooldown_multipliers"],
}


@pytest.mark.parametrize("panel", sorted(FREE_TEXT))
def test_stored_free_text_is_escaped_into_value_attributes(panel: str):
    src = (_PANELS / panel).read_text(encoding="utf-8")
    for key in FREE_TEXT[panel]:
        for m in re.finditer(r'value="\$\{([^}]*\b%s\b[^}]*)\}"' % re.escape(key), src):
            assert "esc(" in m.group(1), (
                f"{panel}: {key} reaches a value attribute unescaped — a quote "
                "in it breaks out of the attribute"
            )


# ── a visible label must actually name its control ──────────────────────


ORPHAN_LABEL = re.compile(
    r"<label>([^<]{1,60})</label>\s*\n\s*<(input|select|textarea)\b((?:(?!>).)*)>",
    re.S,
)


def test_no_visible_label_sits_beside_a_control_it_does_not_name():
    """`field()` pairs a label with its control by id, but only when the field
    is built imperatively. Five panels build the same `.field` markup as a
    template literal, so 26 controls had a visible label contributing nothing
    to their accessible name — a screen reader reached the input and announced
    its type and value with no idea what it set."""
    offenders = []
    for panel in sorted(_PANELS.glob("*.js")):
        src = panel.read_text(encoding="utf-8")
        for m in ORPHAN_LABEL.finditer(src):
            attrs = m.group(3)
            if 'type="checkbox"' in attrs or 'type="radio"' in attrs:
                continue  # these wrap their control, which names it
            if "aria-label=" in attrs or " id=" in attrs:
                continue  # named directly — row editors repeat, so they have
                          # no stable id and carry aria-label instead
            ln = src[: m.start()].count("\n") + 1
            offenders.append(f"{panel.name}:{ln}: <label>{m.group(1).strip()}</label>")
    assert not offenders, (
        "labels with no `for`, beside a control with no `id`:\n" + "\n".join(offenders)
    )


# ── the game-panel shell, which ~10 panels render through ───────────────


def test_tag_chips_can_be_removed_from_a_keyboard():
    src = (_PANELS / "games-panel-shared.js").read_text(encoding="utf-8")
    block = src.split("function makeTagWidget", 1)[1].split("\n  function commit", 1)[0]
    assert 'createElement("button")' in block, "the chip remove is not a button"
    assert 'x.type = "button"' in block, "a bare button inside a form submits it"
    assert 'setAttribute("aria-label"' in block, "the remove control has no name"


def test_numeric_game_options_are_range_checked_before_saving():
    """min/max were rendered as attributes and enforced by nothing: the save is
    a button handler, not a form submit, so native validation never runs. And
    `|| 0` turned a blank field into 0, which for a dial like Minimum Players
    is not a value anybody chose."""
    src = (_PANELS / "games-panel-shared.js").read_text(encoding="utf-8")
    assert "parseInt(el.value, 10) || 0" not in src, "blank still silently becomes 0"
    assert "Number.isFinite(n)" in src
    assert "n < lo || n > hi" in src


# ── the heatmap picks its ink by measurement, not by eye ────────────────


def test_heatmap_ink_is_chosen_from_the_composited_luminance():
    """`intensity > 0.6 ? --bg : --ink-dim` was picked by eye and put every
    cell from 0.25 to 0.75 under AA, bottoming out at 1.94:1 right below the
    switch — the middle of the heatmap."""
    src = (_PANELS / "health-heatmap.js").read_text(encoding="utf-8")
    assert "function cellInk" in src
    assert 'intensity > 0.6 ? "var(--bg)"' not in src
    assert "0.1955" in src, "the crossover is the computed one, not a guess"


# ── an irreversible dial asks first ─────────────────────────────────────


def test_shortening_the_anon_audit_window_is_confirmed():
    """The PUT stores a number and deletes nothing — anon_audit_service's
    purge_expired does the deleting on its own schedule. But shortening the
    window still drops moderation history the moment that job next runs, and
    it was a bare <select> change with no confirmation and no undo."""
    src = (_PANELS / "mod-anon-audit.js").read_text(encoding="utf-8")
    assert "confirmDialog" in src, "shortening retention still saves silently"
    # 0 disables purging, so picking it is a lengthening and must not prompt.
    assert "next !== 0" in src, "picking 'keep forever' would prompt as a shortening"
    assert "sel.value = String(current)" in src, "a declined change must roll back"


# ── a control has to say what it does ───────────────────────────────────


def test_the_heat_ceiling_is_named_in_both_panels_that_set_it():
    """games-legitlibs names the tiers (Flirty / Spicy / Filthy / Unhinged);
    games-config sets the same ceiling per channel and rendered a bare
    1/2/3/4, so an admin there chose between four numbers with nothing to say
    what they meant. The names live in the shared module both import."""
    shared = (_PANELS / "games-panel-shared.js").read_text(encoding="utf-8")
    assert "export const TIER_LABELS" in shared
    assert "export const TIER_EMOJI" in shared
    for panel in ("games-config.js", "games-legitlibs.js"):
        src = (_PANELS / panel).read_text(encoding="utf-8")
        assert 'from "./games-panel-shared.js"' in src, f"{panel}: not importing them"
        assert "const TIER_LABELS =" not in src, f"{panel}: kept its own copy"
    cfg = (_PANELS / "games-config.js").read_text(encoding="utf-8")
    assert "TIER_LABELS[t]" in cfg, "the tier select still renders bare numbers"


def test_the_elevated_permissions_checkbox_says_what_it_allows():
    """Its wrapping <label> holds only the ⚠ glyph, so that was the whole
    accessible name. What it means lived in a `title` on the label, which does
    not name the input."""
    src = (_PANELS / "role-menus.js").read_text(encoding="utf-8")
    assert 'aria-label="Allow a role with elevated permissions"' in src


def test_pause_and_resume_leave_a_way_back():
    """Each row renders one button, chosen from is_paused. Acting on it used to
    swap in a past-tense label and disable the button, so undoing needed a page
    reload. The button becomes the opposite action instead — which requires
    delegation, since a listener bound to the element would go on running the
    action the button no longer offers."""
    src = (_PANELS / "wellness-admin.js").read_text(encoding="utf-8")
    assert 'container.addEventListener("click"' in src, "handlers are not delegated"
    assert 'closest("[data-pause-uid], [data-resume-uid]")' in src
    assert 'btn.dataset.resumeUid = uid' in src, "pausing does not offer Resume"
    assert 'btn.dataset.pauseUid = uid' in src, "resuming does not offer Pause"
    assert 'btn.textContent = "Paused"' not in src, "the dead-end label is back"
