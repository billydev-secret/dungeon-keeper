"""Tests for the bios cog's pure-logic helpers.

Covers headline fallback, weighted draw without replacement, the
6000-char total-content shrink, and the per-field 1024 truncation.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.bios.embeds import (
    build_bio_embed,
    build_field_prompt_embed,
    build_question_prompt_embed,
)
from bot_modules.bios.logic import (
    idle_timeout_seconds,
    input_timeout_seconds,
    question_field_name,
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
from bot_modules.core.branding import SECTION_SPACER


def _unspaced(value: str | None) -> str:
    """A field value without the trailing spacer ``apply_section_spacing`` adds.

    Every field but the last carries ``SECTION_SPACER`` for breathing room
    (docs/embed_style_guide.md § Section spacing). These tests assert content,
    not spacing, so they compare against the value with it removed.
    """
    text = value or ""
    return text[: -len(SECTION_SPACER)] if text.endswith(SECTION_SPACER) else text



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


def test_build_bio_embed_escapes_member_text_in_fields_and_answers():
    """A bio can't reformat its own card — both halves are member-typed."""
    payload = BioRenderPayload(
        display_name="Iris",
        avatar_url="",
        headline_value="Iris",
        fields=(
            FieldSnapshot(
                label="Bio", value="**loud**", field_type="paragraph", skipped=False
            ),
        ),
        questions=(
            QuestionSnapshot(question_text="Tree?", answer="_oak_", skipped=False),
        ),
        embed_color=0xC8763E,
        created_at_iso="",
    )
    embed = build_bio_embed(payload)
    assert embed.fields[0].value.startswith("\\*\\*loud\\*\\*")
    assert embed.fields[1].value == "\\_oak\\_"


def test_escaping_happens_before_the_field_cap():
    """Escaping a value already trimmed to 1024 would push it past Discord's
    field cap and 400 the whole card. The escape has to run first so the cap
    measures what actually gets sent."""
    payload = BioRenderPayload(
        display_name="Iris",
        avatar_url="",
        headline_value="Iris",
        fields=(
            FieldSnapshot(
                label="Bio", value="*" * 2000, field_type="paragraph", skipped=False
            ),
        ),
        questions=(
            QuestionSnapshot(question_text="Tree?", answer="_" * 2000, skipped=False),
        ),
        embed_color=0xC8763E,
        created_at_iso="",
    )
    embed = build_bio_embed(payload)
    for field in embed.fields:
        assert len(field.value or "") <= 1024, len(field.value or "")


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


# ── Discord's 256-char cap on embed field names and titles ────────────
#
# `bio_questions.prompt` accepts 512 chars through the dashboard API, and the
# rendered name is "› " + the prompt. Overflowing the cap makes Discord reject
# the whole embed with a 400, which the wizard turns into a silent
# `cancel("error")` — the member's channel vanishes with nothing saved and no
# message they'd understand.

_LONG_PROMPT = "Q" * 512


def test_question_field_name_fits_after_the_arrow_prefix():
    payload = BioRenderPayload(
        display_name="Iris",
        avatar_url="",
        headline_value="Iris",
        fields=(),
        questions=(
            QuestionSnapshot(question_text=_LONG_PROMPT, answer="oak", skipped=False),
        ),
        embed_color=0xC8763E,
        created_at_iso="",
    )
    embed = build_bio_embed(payload)
    name = embed.fields[0].name
    assert name.startswith("› ")
    assert len(name) <= 256, f"embed field name is {len(name)} chars"


def test_cap_question_answers_leaves_room_for_the_prefix():
    [capped] = cap_question_answers_for_embed(
        [QuestionSnapshot(question_text=_LONG_PROMPT, answer="a", skipped=False)]
    )
    assert len(f"› {capped.question_text}") <= 256


def test_question_field_name_helper_caps_and_preserves_short_text():
    assert question_field_name("Favorite tree?") == "› Favorite tree?"
    assert len(question_field_name(_LONG_PROMPT)) <= 256


# ── Step timeouts must outlive the idle watchdog ──────────────────────
#
# The views used to hardcode 900s while `bios_wizard_timeout` is configurable
# up to 120 minutes. Past 15 minutes every button returned "This interaction
# failed" while the watchdog slept on, so a member could be stuck with no way
# forward and no explanation. Whatever the configured window, the watchdog has
# to fire first — it is the only path that tells the member what happened.


@pytest.mark.parametrize("minutes", [1, 5, 15, 30, 60, 120])
def test_input_windows_outlive_the_idle_watchdog(minutes):
    idle = idle_timeout_seconds(minutes)
    assert idle == minutes * 60
    assert input_timeout_seconds(minutes) > idle


def test_input_window_tracks_the_configured_timeout():
    """The bug: a fixed window that ignores config. 120 minutes configured
    must not leave buttons dying at 15."""
    assert input_timeout_seconds(120) > 120 * 60
    assert input_timeout_seconds(120) > input_timeout_seconds(15)


# ── wizard prompt embeds: the overwrite warning ───────────────────────
#
# The wizard captures a plain channel message, not a modal, so there is no
# pre-filled input to make "this replaces what's there" self-evident. Members
# were losing answers by typing an addition to a question they had already
# answered. The prompt has to say so in words.


def _field_prompt(field, *, prior="", editing=True):
    return build_field_prompt_embed(
        field,
        prior=prior,
        editing=editing,
        step_index=0,
        total_steps=3,
        embed_color=0x5865F2,
    )


def _question_prompt(existing):
    return build_question_prompt_embed(
        _q(1, "What's your comfort food?"),
        existing=existing,
        answered_count=0,
        target_questions=3,
        embed_color=0x5865F2,
    )


def _field_values(embed):
    return {f.name: _unspaced(f.value) for f in embed.fields}


def test_question_prompt_warns_that_a_reply_overwrites():
    embed = _question_prompt("Cold pizza, no notes.")
    assert "replaces" in (embed.description or "")
    # Back is this step's only non-destructive exit — it has no Keep button.
    assert "Back" in (embed.description or "")


def test_question_prompt_shows_the_current_answer():
    embed = _question_prompt("Cold pizza, no notes.")
    assert _field_values(embed)["Current Answer"] == "Cold pizza, no notes."


def test_question_prompt_stays_quiet_for_a_first_answer():
    embed = _question_prompt("")
    assert "replaces" not in (embed.description or "")
    assert "Current Answer" not in _field_values(embed)


def test_question_prompt_truncates_a_long_current_answer():
    embed = _question_prompt("x" * 2000)
    assert len(_field_values(embed)["Current Answer"]) == 1024


def test_field_prompt_warns_and_names_keep_when_editing_over_a_value():
    embed = _field_prompt(_field(1, "Pronouns"), prior="she/her")
    assert "replaces" in (embed.description or "")
    assert "Keep" in (embed.description or "")
    assert _field_values(embed)["Current Answer"] == "she/her"


def test_field_prompt_keeps_its_type_guidance_alongside_the_warning():
    embed = _field_prompt(_field(1, "About you", field_type="paragraph"), prior="hi")
    assert "replaces" in (embed.description or "")
    assert "Take a few sentences" in (embed.description or "")


def test_field_prompt_warning_says_picking_for_a_choice_field():
    """A choice field is answered by a dropdown, not a message — "whatever you
    send" would name a gesture that step doesn't have."""
    embed = _field_prompt(_field(1, "Vibe", field_type="choice"), prior="chaotic")
    assert "Picking a new option" in (embed.description or "")


@pytest.mark.parametrize(
    ("prior", "editing"),
    [
        ("she/her", False),  # first run: nothing saved yet to overwrite
        ("", True),          # editing, but this field was left blank
        ("", False),
    ],
)
def test_field_prompt_stays_quiet_when_theres_nothing_to_overwrite(prior, editing):
    embed = _field_prompt(_field(1, "Pronouns"), prior=prior, editing=editing)
    assert "replaces" not in (embed.description or "")
    assert "Current Answer" not in _field_values(embed)
