"""Shared embed helpers — the field-cap guard and the monospace table cell.

``services/embeds.py`` is the module docs/embed_style_guide.md names, and it
had no test file; ``fit_lines`` arrived here from economy/view_helpers when it
turned out three features hand-roll the same overflow trim.
"""
from __future__ import annotations

from bot_modules.services.embeds import EMBED_FIELD_LIMIT, fit_lines, pad_cell


def test_fit_lines_keeps_leading_rows_under_the_field_cap():
    assert fit_lines(["a", "b", "c"]) == "a\nb\nc"
    # Ten max-length rows must not overrun the 1024-char embed field — an
    # over-long field makes Discord reject the whole embed, not just the field.
    fat = [("x" * 200) for _ in range(10)]
    out = fit_lines(fat)
    assert len(out) <= EMBED_FIELD_LIMIT
    assert out.startswith("x")


def test_fit_lines_counts_the_joining_newlines():
    """The separator is part of the budget, so a limit can't be overshot by N-1."""
    out = fit_lines(["ab", "cd", "ef"], limit=5)
    assert out == "ab\ncd"  # 2 + 1 + 2 = 5; a third row would be 8


def test_fit_lines_drops_a_single_oversized_row_rather_than_truncating_it():
    assert fit_lines(["x" * 50], limit=10) == ""


def test_fit_lines_respects_a_custom_limit():
    assert fit_lines(["a", "b"], limit=1) == "a"


def test_pad_cell_pads_and_clips_to_one_width():
    assert pad_cell("ab", 5) == "ab   "
    assert pad_cell("abcdef", 4) == "abc…"
    assert len(pad_cell("abcdef", 4)) == 4
