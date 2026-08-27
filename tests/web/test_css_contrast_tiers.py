"""The saturated/text colour split in app.css is a contract, not a preference.

`:root` defines two tiers of red and green and documents the rule in a comment:
the saturated `--red` / `--green` are for **borders, badges and fills**, and the
lightened `--red-text` / `--green-text` are for **anywhere the colour carries
meaning as words**. The reason is arithmetic — as 13-14px text the saturated
pair measure 3.35:1 and 3.97:1, under the WCAG AA floor of 4.5:1.

The rule was written, and then 23 declarations across app.css and
help-panel.css used the saturated pair as a text colour anyway — including the
Tickets status chips (3.34:1), the resolved chip (3.80:1), and `.error` /
`.save-err` / `.num-err`, which is to say the error messages. Nothing caught it
because low contrast doesn't throw; it just quietly excludes people.

So the rule is pinned twice here: once as "don't write the saturated token as a
text colour", and once as the actual contrast arithmetic on the token pairs, so
that retuning a hex in `:root` can't quietly push a combination back under the
floor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static"
_SHEETS = [_STATIC / "app.css", _STATIC / "help-panel.css"]
_JS = _STATIC / "js"

# Not preceded by a hyphen or word char — that exemption is what lets
# `border-left-color: var(--red)` stay saturated, which is correct: a 4px
# stripe is a fill, not text.
_TEXT_COLOUR = re.compile(r"(?<![-\w])color:\s*var\(--(red|green)\)")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


@pytest.mark.parametrize("sheet", _SHEETS, ids=lambda p: p.name)
def test_no_saturated_red_or_green_as_text(sheet):
    css = _strip_comments(sheet.read_text(encoding="utf-8"))
    offenders = []
    for i, line in enumerate(css.splitlines(), 1):
        if _TEXT_COLOUR.search(line):
            offenders.append(f"{sheet.name}:{i}: {line.strip()}")
    assert not offenders, (
        "saturated red/green used as a text colour — use --red-text / "
        "--green-text (see :root in app.css):\n" + "\n".join(offenders)
    )


# ── the same rule, in the place it was actually broken ──────────────────────
#
# The sweep above scans stylesheets. Its regex would have matched the JS
# offenders verbatim; panel JS was simply out of scope. So the rule held in
# app.css and broke in inline styles built from template literals — which is
# where most of this dashboard's colour decisions are actually made.
#
# This one is strict by default: **any** saturated token written in JS is an
# offender unless the line says it is a fill. A first draft matched only the
# obvious shapes — a literal `color:`, an assignment to `.style.color`, and a
# local holding "var(--red)" — and missed live-log.js entirely, because its
# saturated red sits in a multi-line object literal and reaches the DOM through
# a destructured loop variable. Enumerating the ways a value can travel is a
# losing game; requiring the fill to declare itself is not.

_JS_TOKEN = re.compile(r"var\(--(red|green)\)")

# Properties that legitimately take the saturated tier. A line carrying one of
# these is a fill, and fills are exactly what the saturated tokens are for.
_FILL_CONTEXT = re.compile(
    r"background|border(?!-radius)|\bfill\b|stroke|box-shadow|accent-color|outline"
)

# Declarations whose values reach a fill somewhere other than their own line —
# a colour map read later as `background:${…}`, or a helper whose return value
# is. Keyed by (file, declaration name); every entry needs a reason, and
# test_the_indirect_fill_exemptions_are_real checks each still reaches a fill.
_INDIRECT_FILLS = {
    ("tiles/tile-helpers.js", "BADGE_COLORS"):
        "badgeHTML renders it as background: on .health-tile-badge",
    ("tiles/composite-score.js", "color"):
        "used as background: on .health-dim-fill",
    ("tiles/channel-health.js", "BAR_FILL"):
        "passed as the bar-fill colour option to miniBarHTML",
    ("panels/channels.js", "STATUS_COLORS"):
        "rendered as background: on .health-tile-badge",
    ("panels/system-stats.js", "pctColor"):
        "its return feeds pctBar, which renders it as background:",
}


def _exempt_ranges(rel: str, src: str) -> list[range]:
    """Line ranges covered by this file's exempted declarations."""
    out = []
    lines = src.splitlines()
    for (f, name), _why in _INDIRECT_FILLS.items():
        if f != rel:
            continue
        for i, line in enumerate(lines):
            if not re.search(r"\b(?:const|let|var|function)\s+" + re.escape(name) + r"\b", line):
                continue
            # Single-line declaration, or a braced block: walk to its close.
            depth = line.count("{") - line.count("}")
            j = i
            while depth > 0 and j + 1 < len(lines):
                j += 1
                depth += lines[j].count("{") - lines[j].count("}")
            out.append(range(i + 1, j + 2))
    return out


def _js_files():
    return sorted(
        p for p in _JS.rglob("*.js")
        if "vendor" not in p.parts and "node_modules" not in p.parts
    )


def test_no_saturated_red_or_green_as_text_in_panel_js():
    offenders = []
    for path in _js_files():
        rel = path.relative_to(_JS).as_posix()
        src = path.read_text(encoding="utf-8")
        exempt = _exempt_ranges(rel, src)
        for i, line in enumerate(src.splitlines(), 1):
            if not _JS_TOKEN.search(line):
                continue
            if _FILL_CONTEXT.search(line):
                continue
            if any(i in r for r in exempt):
                continue
            offenders.append(f"{rel}:{i}: {line.strip()[:95]}")
    assert not offenders, (
        "saturated red/green in panel JS outside a fill context — use "
        "--red-text / --green-text for anything that renders as words. If it "
        "really is a fill, say so on the line (background/border/fill/stroke) "
        "or add its declaration to _INDIRECT_FILLS with a reason:\n"
        + "\n".join(offenders)
    )


def test_the_indirect_fill_exemptions_are_real():
    """An exemption that no longer names a fill has become a hole."""
    for (rel, name), why in _INDIRECT_FILLS.items():
        src = (_JS / rel).read_text(encoding="utf-8")
        assert re.search(r"\b(?:const|let|var|function)\s+" + re.escape(name) + r"\b", src), (
            f"{rel}: `{name}` is gone; drop the exemption"
        )
        assert re.search(r"(background|fill|stroke)", src), (
            f"{rel}: nothing in this file is a fill any more ({why})"
        )


def test_the_js_sweep_can_see_the_shapes_it_claims_to():
    """Guard the guard, against the shapes that actually occurred."""
    assert _JS_TOKEN.search('`<span style="color:var(--red)">x</span>`')
    assert _JS_TOKEN.search('el.style.color = "var(--green)";')
    assert _JS_TOKEN.search('const c = ok ? "var(--green)" : "var(--red)";')
    assert _JS_TOKEN.search('  "ERROR": "var(--red)",')   # the one the first draft missed
    # ...and a fill declares itself on its own line.
    assert _FILL_CONTEXT.search('`<div style="border-color: var(--red)">`')
    assert _FILL_CONTEXT.search('`<div style="background:var(--green)">`')
    assert not _FILL_CONTEXT.search('`<span style="color:var(--red)">`')
    # border-radius must not read as a fill context on its own.
    assert not _FILL_CONTEXT.search('border-radius:4px;color:var(--red)')


# ── the arithmetic behind the rule ──────────────────────────────────────────


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _luminance(rgb: tuple[int, int, int]) -> float:
    def chan(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _composite(fg: tuple[int, int, int], alpha: float, bg: tuple[int, int, int]):
    return tuple(round(fg[i] * alpha + bg[i] * (1 - alpha)) for i in range(3))


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _token(name: str) -> str:
    """Read a colour token's value straight out of :root, so the test tracks
    the stylesheet rather than a copy of it that can drift."""
    css = (_STATIC / "app.css").read_text(encoding="utf-8")
    m = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6,8}})\s*;", css)
    assert m, f"token --{name} not found in app.css"
    return m.group(1)


AA = 4.5


@pytest.mark.parametrize(
    ("text_token", "tint_token", "surface_token", "label"),
    [
        pytest.param("red-text", "red-soft", "bg-card", "danger chip on a card", id="red-chip"),
        pytest.param("green-text", "green-soft", "bg-card", "success chip on a card", id="green-chip"),
    ],
)
def test_text_tier_clears_aa_on_its_tinted_chip(text_token, tint_token, surface_token, label):
    surface = _rgb(_token(surface_token))
    tint = _token(tint_token)
    bg = _composite(_rgb(tint[:7]), int(tint[7:9], 16) / 255, surface)
    ratio = _contrast(bg, _rgb(_token(text_token)))
    assert ratio >= AA, f"{label}: {ratio:.2f}:1 is under the {AA}:1 floor"


@pytest.mark.parametrize(
    ("text_token", "surface_token"),
    [
        pytest.param("red-text", "bg", id="red-on-ground"),
        pytest.param("red-text", "bg-card", id="red-on-card"),
        pytest.param("green-text", "bg", id="green-on-ground"),
        pytest.param("green-text", "bg-card", id="green-on-card"),
    ],
)
def test_text_tier_clears_aa_as_plain_text(text_token, surface_token):
    """`.error`, `.save-err` and friends sit on a bare surface, not a chip."""
    ratio = _contrast(_rgb(_token(surface_token)), _rgb(_token(text_token)))
    assert ratio >= AA, f"--{text_token} on --{surface_token}: {ratio:.2f}:1"


def test_saturated_tier_would_still_fail_ie_the_rule_is_load_bearing():
    """If this ever passes, the two tiers have converged and the rule is moot.

    Guards against someone 'simplifying' by pointing --red at --red-text: that
    would silently weaken every border and fill that wants the saturated hue.
    """
    ratio = _contrast(_rgb(_token("bg")), _rgb(_token("red")))
    assert ratio < AA, (
        "--red now clears AA as text, so the two-tier split no longer earns "
        "its keep — re-check whether --red-text is still needed"
    )


# ── the destructive-button rule ─────────────────────────────────────────
#
# docs/dashboard_visual_language.md: "A destructive action is never the filled
# one." `.act-btn.danger` honoured it; `.btn-danger` — the other button kit,
# and the one with 33 uses against that kit's two — was a solid `--red` fill
# with white text, which is both the loudest control on the page and 3.77:1.
#
# The one exception the rule names is a confirm dialog, where a decision is
# actually being taken. That is why the solid treatment is scoped to
# `.confirm-box` rather than deleted.


def _rule_body(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"{selector} not found in app.css"
    return m.group(1)


def test_the_in_page_destructive_button_is_outlined():
    css = _strip_comments((_STATIC / "app.css").read_text(encoding="utf-8"))
    body = _rule_body(css, ".btn-danger")
    assert "background: transparent" in body, (
        "in-page .btn-danger is a filled button — see the destructive-action "
        "rule in docs/dashboard_visual_language.md"
    )
    assert "var(--red-text)" in body, "outlined danger uses the --red-text tier"


def test_the_confirm_dialogs_button_stays_solid_and_clears_aa():
    css = _strip_comments((_STATIC / "app.css").read_text(encoding="utf-8"))
    body = _rule_body(css, ".confirm-box .btn-danger")
    m = re.search(r"background:\s*(#[0-9a-fA-F]{6})", body)
    assert m, "the confirm dialog's danger button has no solid fill"
    assert "color: #fff" in body
    # .btn is var(--t-2) — 12.5px, so this is normal text at the 4.5:1 floor.
    ratio = _contrast(_rgb("#ffffff"), _rgb(m.group(1)))
    assert ratio >= 4.5, (
        f"white on {m.group(1)} is {ratio:.2f}:1, under AA for 12.5px text"
    )
