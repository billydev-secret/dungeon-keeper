"""The one place the ``▰▱`` progress-meter vocabulary is defined.

Every text meter in the bot — game vote bars, quest and community-goal
progress, the casino's implied-odds bar, the chicken meter, the pressure
gauge, the privacy scan/delete counters — draws from here, so changing the
glyphs is a one-line edit rather than an eight-module sweep.

**Why meters must render inside a code span.** ``▰`` (U+25B0 BLACK
PARALLELOGRAM) and ``▱`` (U+25B1 WHITE PARALLELOGRAM) do *not* have the same
advance width in Discord's proportional font stack — the outlined glyph is
wider. A bar is always a fixed number of characters, but a bare one still
gets visibly *shorter* as it fills, because filled glyphs are narrower than
the empty ones they replace. An all-empty 0% bar next to a 50% bar reads as
two different lengths and looks like a bug.

Wrapping the run in a code span forces a monospace font, where every glyph
shares one advance, so equal character counts render as equal pixel lengths.
Hence the split below: :func:`fill` returns the *raw* glyphs and never
wraps, and :func:`mono` does the wrapping at the display site. Two callers
(the ``/bank quests`` table cell and the login digest's meter) compose the
fill into a code span they build themselves, and would end up with nested
backticks if the primitive wrapped for them.
"""

from __future__ import annotations

BAR_FILLED = "▰"
BAR_EMPTY = "▱"


def fill(current: float, target: float, width: int) -> str:
    """The raw meter glyphs for ``current`` out of ``target`` — no numbers,
    no code span.

    A non-positive ``target`` reads as empty. Callers wanting a different
    policy decide it themselves: the privacy scanner renders a *full* bar
    when there is nothing to delete, because the run is already complete.

    ``current`` is a float so percentage-driven meters (the chicken meter,
    the pressure gauge) share this fill instead of hand-rolling it.
    """
    if target <= 0:
        return BAR_EMPTY * width
    filled = max(0, min(width, round(width * current / target)))
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def mono(text: str) -> str:
    """Wrap a rendered meter in a code span so it renders monospace.

    Apply this at the display site, to the bar *and* any counts that should
    stay column-aligned with it. Skip it when the meter is already being
    composed into a caller-built code span — backticks do not nest.
    """
    return f"`{text}`"
