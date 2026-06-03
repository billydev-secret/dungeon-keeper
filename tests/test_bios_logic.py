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
    WizardState,
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


# ── WizardState transitions ───────────────────────────────────────────


def test_wizard_state_field_phase():
    fields = [_field(1, sort_order=0), _field(2, sort_order=1)]
    s = WizardState(mode="new", fields=fields, target_questions=3)
    assert s.step_kind() == "field"
    s.step_index = 1
    assert s.step_kind() == "field"
    s.step_index = 2
    assert s.step_kind() == "question_browse"


def test_wizard_state_pending_question_routes_to_answer():
    s = WizardState(mode="new", fields=[], target_questions=3)
    s.pending_question = _q(1)
    assert s.step_kind() == "question_answer"


def test_wizard_state_done_at_target():
    s = WizardState(mode="new", fields=[], target_questions=2)
    s.question_answers = [(_q(1), "a")]
    assert s.step_kind() == "question_browse"
    s.question_answers.append((_q(2), "b"))
    assert s.step_kind() == "done"


def test_wizard_state_explicit_done_short_circuits():
    s = WizardState(mode="new", fields=[], target_questions=5)
    s.questions_complete = True
    assert s.step_kind() == "done"


def test_wizard_state_answered_question_ids():
    s = WizardState(mode="new", fields=[], target_questions=3)
    s.question_answers = [(_q(1), "a"), (_q(2), "b")]
    assert s.answered_question_ids == {1, 2}


def test_wizard_state_total_steps_progress_chip():
    s = WizardState(
        mode="new",
        fields=[_field(1), _field(2), _field(3)],
        target_questions=3,
    )
    assert s.total_steps == 6


def test_wizard_state_back_within_fields_only():
    """The browse view passes Back through `step_index`; the apply
    logic should leave field walking intact when the user crosses back."""
    s = WizardState(
        mode="new", fields=[_field(1), _field(2)], target_questions=2
    )
    s.step_index = 2  # after fields
    assert s.step_kind() == "question_browse"
    # Simulate the "Back to fields" action effect:
    s.step_index = len(s.fields) - 1
    assert s.step_kind() == "field"


# ── Resurrect: payload reconstruction from stored snapshot ──────────


def test_build_payload_from_stored_orders_by_sort_order():
    from bot_modules.bios.db import StoredBio
    from bot_modules.bios.resurrect import build_payload_from_stored

    stored = StoredBio(
        user_id=1,
        guild_id=2,
        message_id=0,  # archived
        channel_id=0,
        created_at="2026-06-02T00:00:00",
        updated_at="2026-06-02T00:00:00",
        field_values={
            10: ("Name", "Iris"),
            11: ("Bio", "Hello world"),
        },
        answers={
            0: (100, "Favorite tree?", "Oak"),
            1: (101, "Pet peeve?", "Loud chewing"),
        },
    )
    field_meta = {
        # sort_order, field_type, is_headline
        10: (0, "short", True),
        11: (1, "paragraph", False),
    }
    payload = build_payload_from_stored(
        stored,
        member_display_name="Iris",
        member_avatar_url="http://x/y.png",
        field_meta=field_meta,
        embed_color=0xC8763E,
    )
    assert payload.headline_value == "Iris"
    assert [f.label for f in payload.fields] == ["Name", "Bio"]
    assert payload.fields[0].field_type == "short"
    assert payload.fields[1].field_type == "paragraph"
    assert [q.question_text for q in payload.questions] == [
        "Favorite tree?",
        "Pet peeve?",
    ]


def test_build_payload_from_stored_falls_back_when_no_headline():
    from bot_modules.bios.db import StoredBio
    from bot_modules.bios.resurrect import build_payload_from_stored

    stored = StoredBio(
        user_id=1,
        guild_id=2,
        message_id=0,
        channel_id=0,
        created_at="2026-06-02T00:00:00",
        updated_at="2026-06-02T00:00:00",
        field_values={10: ("Name", "Iris")},
        answers={},
    )
    payload = build_payload_from_stored(
        stored,
        member_display_name="Iris",
        member_avatar_url="",
        field_meta={10: (0, "short", False)},
        embed_color=0,
    )
    # No headline flagged → fallback to first field by sort_order.
    assert payload.headline_value == "Iris"


# ── build_bio_embed integration ──────────────────────────────────────


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
