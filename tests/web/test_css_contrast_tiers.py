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
