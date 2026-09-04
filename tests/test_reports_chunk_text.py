"""Coverage for the shared message chunker in ``core/reports``.

It had none before it was generalised for voice transcripts, and it is now
shared by three unrelated callers (ephemeral report text, the hidden-channels
listing, and voice-note transcripts), so the line-splitting behaviour the
older two rely on is pinned here rather than assumed.
"""
from __future__ import annotations

import pytest

from bot_modules.core.reports import SAFE_TEXT_CHUNK, chunk_text


# ── the line-splitting default the original callers depend on ────────────────


def test_short_text_is_one_chunk():
    assert chunk_text("walk the site") == ["walk the site"]


def test_empty_text_yields_one_empty_chunk():
    """Pinned as-is: send_ephemeral_text has always iterated over this."""
    assert chunk_text("") == [""]


def test_a_long_text_splits_on_line_boundaries():
    text = ("a line of report output\n" * 200).rstrip()
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= SAFE_TEXT_CHUNK for c in chunks)
    assert all(c.startswith("a line") and c.endswith("output") for c in chunks)
    assert "\n".join(chunks) == text


def test_a_line_longer_than_the_limit_is_hard_split():
    """No boundary to find, so the cut has to fall mid-line."""
    chunks = chunk_text("x" * 4000)
    assert len(chunks) == 3
    assert all(len(c) <= SAFE_TEXT_CHUNK for c in chunks)
    assert "".join(chunks) == "x" * 4000


@pytest.mark.parametrize(
    "length,expected",
    [
        pytest.param(SAFE_TEXT_CHUNK - 1, 1, id="just-under"),
        pytest.param(SAFE_TEXT_CHUNK, 1, id="exactly-the-limit"),
        pytest.param(SAFE_TEXT_CHUNK + 1, 2, id="one-over"),
    ],
)
def test_the_limit_is_the_boundary_not_an_approximation(length, expected):
    assert len(chunk_text("x" * length)) == expected


# ── boundary and min_fill ────────────────────────────────────────────────────


def test_a_space_boundary_keeps_whole_words():
    """What a voice transcript needs: prose with no newlines to cut on."""
    chunks = chunk_text(("alpha " * 2000).strip(), boundary=" ", min_fill=0.8)
    assert len(chunks) > 1
    assert all(c.startswith("alpha") and c.endswith("alpha") for c in chunks)
    assert " ".join(chunks) == ("alpha " * 2000).strip()


def test_the_final_chunk_keeps_the_input_trailing_whitespace():
    """Pinned, not fixed: this is what the helper has always done, and a
    trailing space is invisible in Discord. Callers that care (the transcript
    splitter) strip before calling rather than making every caller pay."""
    assert chunk_text("alpha " * 2000, boundary=" ", min_fill=0.8)[-1].endswith(" ")


def test_min_fill_rejects_a_boundary_that_would_waste_the_message():
    """A separator near the start is worse than a clean cut at the limit."""
    text = "tiny " + "x" * 4000
    packed = chunk_text(text, boundary=" ", min_fill=0.8)
    assert len(packed[0]) > SAFE_TEXT_CHUNK * 0.8

    # With no floor the same lone boundary is taken, and the message is wasted.
    wasteful = chunk_text(text, boundary=" ", min_fill=0.0)
    assert wasteful[0] == "tiny"


# ── prefix ───────────────────────────────────────────────────────────────────


def test_the_prefix_rides_on_the_first_chunk_only():
    chunks = chunk_text("alpha " * 2000, boundary=" ", min_fill=0.8, prefix="HEAD: ")
    assert chunks[0].startswith("HEAD: ")
    assert not any(c.startswith("HEAD: ") for c in chunks[1:])


def test_the_prefix_is_paid_for_out_of_the_first_chunk_budget():
    """Otherwise a header pushes the message Discord measures over the cap."""
    prefix = "H" * 200
    chunks = chunk_text("alpha " * 2000, boundary=" ", min_fill=0.8, prefix=prefix)
    assert all(len(c) <= SAFE_TEXT_CHUNK for c in chunks)
    assert len(chunks[0]) <= SAFE_TEXT_CHUNK


def test_an_empty_text_with_a_prefix_is_just_the_prefix():
    assert chunk_text("", prefix="HEAD: ") == ["HEAD: "]


# ── max_parts and overflow_note ──────────────────────────────────────────────


def test_max_parts_bounds_the_result_and_marks_the_cut():
    chunks = chunk_text(
        "alpha " * 20_000, boundary=" ", min_fill=0.8, max_parts=10, overflow_note="[cut]"
    )
    assert len(chunks) == 10
    assert all(len(c) <= SAFE_TEXT_CHUNK for c in chunks)
    assert chunks[-1].endswith("[cut]")
    assert not any(c.endswith("[cut]") for c in chunks[:-1])


def test_the_overflow_note_is_paid_for_out_of_the_budget():
    """Appending it on top is what pushes a message over the real cap."""
    note = "!" * 100
    chunks = chunk_text("x" * 9000, max_parts=2, overflow_note=note)
    assert len(chunks) == 2
    assert all(len(c) <= SAFE_TEXT_CHUNK for c in chunks)


def test_a_cap_that_is_not_reached_adds_no_note():
    chunks = chunk_text("alpha " * 100, boundary=" ", max_parts=10, overflow_note="[cut]")
    assert len(chunks) == 1
    assert not chunks[0].endswith("[cut]")


def test_a_cap_of_one_is_a_plain_fit():
    chunks = chunk_text("x" * 4000, max_parts=1, overflow_note="[cut]")
    assert len(chunks) == 1
    assert len(chunks[0]) <= SAFE_TEXT_CHUNK
    assert chunks[0].endswith("[cut]")


def test_a_zero_or_negative_limit_still_terminates():
    """Defensive: a prefix longer than the limit must not spin forever."""
    assert len(chunk_text("some text here", limit=0)) >= 1
    assert len(chunk_text("some text here", limit=5, prefix="X" * 50)) >= 1
