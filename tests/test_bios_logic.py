"""Tests for the bios cog's pure-logic helpers.

Covers headline fallback, weighted draw without replacement, the
6000-char total-content shrink, and the per-field 1024 truncation.
"""

from __future__ import annotations

import random

from bot_modules.bios.embeds import build_bio_embed
from bot_modules.bios.logic import (
    BioField,
    BioQuestion,
    BioRenderPayload,
    FieldSnapshot,
    QuestionSnapshot,
    cap_field_values_for_embed,
    cap_question_answers_for_embed,
    draw_weighted,
    headline_value,
    shrink_to_embed_total,
    truncate,
)


def _field(
    fid: int,
    label: str = "Field",
    *,
    is_headline: bool = False,
    sort_order: int = 0,
    field_type: str = "short",
) -> BioField:
    return BioField(
        id=fid,
        label=label,
        field_type=field_type,  # type: ignore[arg-type]
        choices=(),
        required=False,
        is_headline=is_headline,
        sort_order=sort_order,
        max_len=1024,
    )


def _q(qid: int, prompt: str = "P", weight: int = 1) -> BioQuestion:
    return BioQuestion(id=qid, prompt=prompt, weight=weight)


# ── headline_value ────────────────────────────────────────────────────


def test_headline_prefers_flagged_field():
    fields = [
        _field(1, "Name", is_headline=True, sort_order=2),
        _field(2, "Other", sort_order=0),
    ]
    value, fid = headline_value(fields, {1: "Iris", 2: "x"})
    assert value == "Iris"
    assert fid == 1


def test_headline_falls_back_to_first_by_sort_order_when_unflagged():
    fields = [
        _field(2, "Pronouns", sort_order=1),
        _field(1, "Name", sort_order=0),
    ]
    value, fid = headline_value(fields, {1: "Iris", 2: "she/her"})
    assert value == "Iris"
    assert fid == 1


def test_headline_returns_dash_when_no_fields():
    value, fid = headline_value([], {})
    assert value == "—"
    assert fid is None


def test_headline_returns_dash_when_answer_missing():
    fields = [_field(1, is_headline=True)]
    value, fid = headline_value(fields, {})
    assert value == "—"
    assert fid == 1


# ── draw_weighted ─────────────────────────────────────────────────────


def test_draw_weighted_returns_distinct():
    pool = [_q(i) for i in range(1, 6)]
    rng = random.Random(0)
    drawn = draw_weighted(pool, 3, rng=rng)
    assert len(drawn) == 3
    assert len({q.id for q in drawn}) == 3


def test_draw_weighted_caps_at_pool_size():
    pool = [_q(1), _q(2)]
    drawn = draw_weighted(pool, 5, rng=random.Random(0))
    assert len(drawn) == 2


def test_draw_weighted_honors_excludes():
    pool = [_q(i) for i in range(1, 5)]
    drawn = draw_weighted(
        pool, 4, exclude_ids=frozenset({1, 2}), rng=random.Random(0)
    )
    assert {q.id for q in drawn} == {3, 4}


def test_draw_weighted_returns_empty_when_pool_empty():
    assert draw_weighted([], 3, rng=random.Random(0)) == []


def test_draw_weighted_respects_weights():
    """A heavily-weighted question should dominate over many trials."""
    pool = [_q(1, weight=1), _q(2, weight=100)]
    counts = {1: 0, 2: 0}
    rng = random.Random(0)
    for _ in range(500):
        drawn = draw_weighted(pool, 1, rng=rng)
        counts[drawn[0].id] += 1
    assert counts[2] > counts[1] * 5


# ── truncate ──────────────────────────────────────────────────────────


def test_truncate_no_op_under_limit():
    assert truncate("hello", 10) == "hello"


def test_truncate_adds_ellipsis_when_over():
    assert truncate("hello world", 8).endswith("…")
    assert len(truncate("hello world", 8)) == 8


# ── caps ──────────────────────────────────────────────────────────────


def test_cap_field_values_caps_at_1024():
    long = "x" * 2000
    s = FieldSnapshot(label="L", value=long, field_type="paragraph", skipped=False)
    out = cap_field_values_for_embed([s])
    assert len(out[0].value) == 1024
    assert out[0].value.endswith("…")


def test_cap_question_answers_caps_at_1024():
    s = QuestionSnapshot(question_text="q", answer="x" * 2000, skipped=False)
    out = cap_question_answers_for_embed([s])
    assert len(out[0].answer) == 1024


# ── shrink_to_embed_total ─────────────────────────────────────────────


def test_shrink_keeps_under_ceiling():
    fields = [
        FieldSnapshot(label="A", value="x" * 1000, field_type="paragraph", skipped=False),
        FieldSnapshot(label="B", value="y" * 1000, field_type="paragraph", skipped=False),
    ]
    questions = [
        QuestionSnapshot(question_text="q1", answer="z" * 1000, skipped=False),
        QuestionSnapshot(question_text="q2", answer="z" * 1000, skipped=False),
        QuestionSnapshot(question_text="q3", answer="z" * 1000, skipped=False),
    ]
    out_f, out_q = shrink_to_embed_total(fields, questions, ceiling=4000)
    total = sum(len(s.label) + len(s.value) for s in out_f) + sum(
        len(s.question_text) + len(s.answer) for s in out_q
    )
    assert total <= 4000


def test_shrink_no_op_when_under_ceiling():
    fields = [FieldSnapshot(label="A", value="x", field_type="short", skipped=False)]
    questions = [QuestionSnapshot(question_text="q", answer="a", skipped=False)]
    out_f, out_q = shrink_to_embed_total(fields, questions, ceiling=6000)
    assert out_f == fields
    assert out_q == questions


# ── build_bio_embed (light end-to-end) ────────────────────────────────


def test_build_bio_embed_inline_for_short_and_choice():
    payload = BioRenderPayload(
        display_name="Iris",
        avatar_url="http://x/y.png",
        headline_value="Iris",
        fields=(
            FieldSnapshot(label="Name", value="Iris", field_type="short", skipped=False),
            FieldSnapshot(
                label="Pronouns", value="she/her", field_type="choice", skipped=False
            ),
            FieldSnapshot(label="Bio", value="hello world", field_type="paragraph", skipped=False),
        ),
        questions=(),
        embed_color=0xC8763E,
        created_at_iso="2026-06-01T12:00:00",
    )
    embed = build_bio_embed(payload)
    assert embed.title == "Iris"
    assert embed.color is not None and embed.color.value == 0xC8763E
    assert [f.inline for f in embed.fields] == [True, True, False]


def test_build_bio_embed_skips_empty_fields():
    payload = BioRenderPayload(
        display_name="Iris",
        avatar_url="",
        headline_value="Iris",
        fields=(
            FieldSnapshot(label="Name", value="Iris", field_type="short", skipped=False),
            FieldSnapshot(label="Hobby", value="", field_type="short", skipped=True),
        ),
        questions=(),
        embed_color=0xC8763E,
        created_at_iso="",
    )
    embed = build_bio_embed(payload)
    assert [f.name for f in embed.fields] == ["Name"]


def test_build_bio_embed_question_uses_arrow_prefix():
    payload = BioRenderPayload(
        display_name="Iris",
        avatar_url="",
        headline_value="Iris",
        fields=(),
        questions=(
            QuestionSnapshot(
                question_text="Favorite tree?",
                answer="oak",
                skipped=False,
            ),
        ),
        embed_color=0xC8763E,
        created_at_iso="",
    )
    embed = build_bio_embed(payload)
    assert embed.fields[0].name == "› Favorite tree?"
    assert embed.fields[0].inline is False
