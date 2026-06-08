"""Pure decision logic for the bios cog — Discord-free and unit-testable.

This module owns the small invariants that the wizard and embed renderer
both depend on: weighted-random question draw, headline fallback, and
the snapshot payload shape that `embeds.build_bio_embed` consumes.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal

FieldType = Literal["short", "paragraph", "choice"]


@dataclass(frozen=True)
class BioField:
    """An active row from bio_fields — what the wizard walks."""

    id: int
    label: str
    field_type: FieldType
    choices: tuple[str, ...]
    required: bool
    is_headline: bool
    sort_order: int
    max_len: int
    hint: str = ""  # admin-authored example/helper shown in the wizard prompt


@dataclass(frozen=True)
class BioQuestion:
    """An active row from bio_questions."""

    id: int
    prompt: str
    weight: int


@dataclass(frozen=True)
class FieldSnapshot:
    """Renderer input for one profile field — already-snapshotted."""

    label: str
    value: str
    field_type: FieldType
    skipped: bool


@dataclass(frozen=True)
class QuestionSnapshot:
    """Renderer input for one icebreaker slot — already-snapshotted."""

    question_text: str
    answer: str
    skipped: bool


@dataclass(frozen=True)
class BioRenderPayload:
    """Everything `build_bio_embed` needs. No Discord lookups required."""

    display_name: str
    avatar_url: str
    headline_value: str
    fields: tuple[FieldSnapshot, ...]
    questions: tuple[QuestionSnapshot, ...]
    embed_color: int
    created_at_iso: str


def headline_value(
    fields: list[BioField], answers: dict[int, str]
) -> tuple[str, int | None]:
    """Pick the headline value from a (field, answer) collection.

    Per spec §7: prefer the field flagged `is_headline=1`. If none is
    flagged (misconfiguration), fall back to the first active field by
    `sort_order`. Returns `(value, field_id)`. `field_id` is None when
    there's literally no field to fall back to.
    """
    flagged = [f for f in fields if f.is_headline]
    if flagged:
        f = flagged[0]
        return answers.get(f.id, "") or "—", f.id
    ordered = sorted(fields, key=lambda f: f.sort_order)
    if not ordered:
        return "—", None
    f = ordered[0]
    return answers.get(f.id, "") or "—", f.id


def draw_weighted(
    pool: list[BioQuestion],
    n: int,
    *,
    exclude_ids: frozenset[int] = frozenset(),
    rng: random.Random | None = None,
) -> list[BioQuestion]:
    """Weighted-random draw without replacement.

    Returns at most `min(n, len(eligible))` distinct questions, sampled
    with weights from `bio_questions.weight`. Pool members in
    `exclude_ids` are filtered out first. If the eligible pool is smaller
    than `n`, returns as many as it can — the caller decides what to do
    (spec §12: "draw as many distinct as exist").
    """
    rng = rng or random.Random()
    eligible = [q for q in pool if q.id not in exclude_ids]
    drawn: list[BioQuestion] = []
    while eligible and len(drawn) < n:
        weights = [max(q.weight, 1) for q in eligible]
        pick = rng.choices(eligible, weights=weights, k=1)[0]
        drawn.append(pick)
        eligible = [q for q in eligible if q.id != pick.id]
    return drawn


def truncate(text: str, limit: int) -> str:
    """Truncate to `limit` chars with a trailing ellipsis if cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def cap_field_values_for_embed(snapshots: list[FieldSnapshot]) -> list[FieldSnapshot]:
    """Apply the 1024-char per-field cap (spec §6.2 safety net)."""
    return [
        FieldSnapshot(
            label=s.label,
            value=truncate(s.value, 1024),
            field_type=s.field_type,
            skipped=s.skipped,
        )
        for s in snapshots
    ]


def cap_question_answers_for_embed(
    snapshots: list[QuestionSnapshot],
) -> list[QuestionSnapshot]:
    """Apply the 1024-char per-answer cap."""
    return [
        QuestionSnapshot(
            question_text=truncate(s.question_text, 256),
            answer=truncate(s.answer, 1024),
            skipped=s.skipped,
        )
        for s in snapshots
    ]


def shrink_to_embed_total(
    fields: list[FieldSnapshot],
    questions: list[QuestionSnapshot],
    *,
    overhead: int = 256,
    ceiling: int = 6000,
) -> tuple[list[FieldSnapshot], list[QuestionSnapshot]]:
    """If the total embed content approaches 6000 chars (Discord limit),
    progressively truncate the longest `paragraph` fields until under
    budget (spec §6.2). `overhead` accounts for title/author/footer.
    """

    def total() -> int:
        f_sum = sum(len(s.label) + len(s.value) for s in fields if not s.skipped)
        q_sum = sum(
            len(s.question_text) + len(s.answer) for s in questions if not s.skipped
        )
        return f_sum + q_sum + overhead

    fields = list(fields)
    questions = list(questions)
    safety = 0
    while total() > ceiling and safety < 64:
        safety += 1
        paragraphs = [
            (i, s)
            for i, s in enumerate(fields)
            if s.field_type == "paragraph" and not s.skipped and len(s.value) > 64
        ]
        if not paragraphs:
            answers = [
                (i, s)
                for i, s in enumerate(questions)
                if not s.skipped and len(s.answer) > 64
            ]
            if not answers:
                break
            i, s = max(answers, key=lambda kv: len(kv[1].answer))
            new_len = max(64, len(s.answer) - max(64, (total() - ceiling) + 16))
            questions[i] = QuestionSnapshot(
                question_text=s.question_text,
                answer=truncate(s.answer, new_len),
                skipped=s.skipped,
            )
            continue
        i, s = max(paragraphs, key=lambda kv: len(kv[1].value))
        new_len = max(64, len(s.value) - max(64, (total() - ceiling) + 16))
        fields[i] = FieldSnapshot(
            label=s.label,
            value=truncate(s.value, new_len),
            field_type=s.field_type,
            skipped=s.skipped,
        )
    return fields, questions


@dataclass
class WizardState:
    """In-memory session state. Owned by WizardSession; mutated in place.

    The wizard runs in two phases:
      1. **Fields** — walks ``fields`` in order using ``step_index``
         (0..len(fields)). User answers each profile field.
      2. **Questions** — once fields are done, the user browses the
         active icebreaker pool and picks questions to answer. Each
         answered question is appended to ``question_answers`` in
         pick-order. The phase ends when the user clicks "Done" or has
         answered ``target_questions`` of them.

    Between the two phases the user can also drop back into fields via
    Back; ``step_index == len(fields)`` is the boundary.
    """

    mode: Literal["new", "edit"]
    fields: list[BioField]
    target_questions: int  # soft cap from config.questions_per_bio
    step_index: int = 0  # 0..len(fields) for the field-walking phase
    field_values: dict[int, str] = field(default_factory=dict)
    field_skipped: set[int] = field(default_factory=set)

    # Question phase — list of (question, answer) in pick order.
    question_answers: list[tuple[BioQuestion, str]] = field(default_factory=list)
    # When set, the user has picked a question and is answering it.
    pending_question: BioQuestion | None = None
    # True once the user explicitly clicks "Done with questions".
    questions_complete: bool = False
    # 0-indexed page within the active pool's paginated browse.
    browse_page: int = 0

    def step_kind(
        self,
    ) -> Literal["field", "question_browse", "question_answer", "done"]:
        if self.step_index < len(self.fields):
            return "field"
        if self.pending_question is not None:
            return "question_answer"
        if (
            self.questions_complete
            or len(self.question_answers) >= self.target_questions
        ):
            return "done"
        return "question_browse"

    def current_field(self) -> BioField | None:
        if self.step_index < len(self.fields):
            return self.fields[self.step_index]
        return None

    @property
    def answered_question_ids(self) -> set[int]:
        return {q.id for (q, _) in self.question_answers}

    @property
    def total_steps(self) -> int:
        """Heuristic count for the progress chip — fields + soft target."""
        return len(self.fields) + self.target_questions
