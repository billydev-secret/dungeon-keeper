"""Embed builders for the bios cog.

Pure: takes a fully-snapshotted `BioRenderPayload` and returns a
`discord.Embed`. No template lookups, no Discord state — what the user
entered at submit time is exactly what renders, even if the template
or question pool changed afterward.

The wizard's *prompt* embeds live here for the same reason: they are pure
functions of (what we're asking, what's already saved), which keeps the
overwrite warning — the thing members actually get bitten by — assertable
without standing up a wizard session.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot_modules.bios.logic import (
    BioField,
    BioQuestion,
    BioRenderPayload,
    FieldSnapshot,
    QuestionSnapshot,
    cap_field_values_for_embed,
    cap_question_answers_for_embed,
    question_field_name,
    shrink_to_embed_total,
)
from bot_modules.core.branding import apply_section_spacing

#: Discord caps a single embed field value at 1024 characters.
_FIELD_VALUE_LIMIT = 1024
#: `bio_fields.hint` is admin-authored; keep the example from crowding the ask.
_HINT_LIMIT = 256


def _parse_timestamp(iso: str) -> datetime | None:
    if not iso:
        return None
    raw = iso.rstrip("Z")
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            dt = parser(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (TypeError, ValueError):
            continue
    return None


def build_bio_embed(payload: BioRenderPayload) -> discord.Embed:
    """Build the styled member-bio embed (spec §6).

    Layout:
        author = display name + avatar icon
        title  = headline value
        thumbnail = avatar
        color  = guild's configured embed color (same across all bios)
        fields = profile fields in order (short/choice inline, paragraph
                 full-width, skipped omitted) then icebreaker answers
                 (full-width, name = ``› {question}``)
        footer = timestamp only
    """
    fields = cap_field_values_for_embed(list(payload.fields))
    questions = cap_question_answers_for_embed(list(payload.questions))
    fields, questions = shrink_to_embed_total(fields, questions)

    embed = discord.Embed(
        title=payload.headline_value or "—",
        color=payload.embed_color,
    )
    embed.set_author(name=payload.display_name, icon_url=payload.avatar_url or None)
    if payload.avatar_url:
        embed.set_thumbnail(url=payload.avatar_url)

    for snap in fields:
        if snap.skipped or not snap.value:
            continue
        inline = snap.field_type in ("short", "choice")
        embed.add_field(
            name=snap.label,
            value=discord.utils.escape_markdown(snap.value),
            inline=inline,
        )

    for snap in questions:
        if snap.skipped or not snap.answer:
            continue
        embed.add_field(
            name=question_field_name(snap.question_text),
            value=snap.answer,
            inline=False,
        )

    ts = _parse_timestamp(payload.created_at_iso)
    if ts is not None:
        embed.timestamp = ts
    apply_section_spacing(embed)
    return embed


def build_field_prompt_embed(
    field: BioField,
    *,
    prior: str,
    editing: bool,
    step_index: int,
    total_steps: int,
    embed_color: int,
) -> discord.Embed:
    """The wizard's ask for one profile field.

    When the member is editing and the field already has a value, the prompt
    leads with the overwrite warning: the wizard captures a plain channel
    message, so there is no modal to pre-fill and nothing else on screen says
    that typing *replaces* rather than appends. **Keep** is the non-destructive
    exit, so the warning names it.
    """
    replacing = editing and bool(prior)
    e = discord.Embed(title=field.label, color=embed_color)
    if replacing:
        e.add_field(
            name="Current answer",
            value=prior[:_FIELD_VALUE_LIMIT],
            inline=False,
        )
    # Warm, type-aware guidance instead of a bare "reply with..." line.
    if field.field_type == "paragraph":
        guidance = "Take a few sentences — no need to be polished, just be you."
    elif field.field_type == "choice":
        guidance = "Pick whichever fits best below — you can change it later."
    else:
        guidance = "A word or a short phrase is perfect."
    if replacing:
        verb = "Picking a new option" if field.field_type == "choice" else "Whatever you send next"
        e.description = (
            f"✏️ **{verb} replaces the answer above** — it isn't added to it. "
            "Tap **Keep** to leave it exactly as it is.\n\n" + guidance
        )
    else:
        e.description = guidance
    # Admin-authored example, if one is set — the biggest unblocker for
    # members who aren't sure what to write.
    if field.hint:
        e.add_field(name="💡 For Example", value=field.hint[:_HINT_LIMIT], inline=False)
    if not field.required:
        e.add_field(
            name="​",
            value="*Totally optional — tap **Skip** if you'd rather not.*",
            inline=False,
        )
    e.set_footer(text=f"Step {step_index + 1} / {total_steps}")
    apply_section_spacing(e)
    return e


def build_question_prompt_embed(
    question: BioQuestion,
    *,
    existing: str,
    answered_count: int,
    target_questions: int,
    embed_color: int,
) -> discord.Embed:
    """The wizard's ask for one icebreaker question.

    Re-picking an answered question (the ✏️ rows in the browse list) lands
    here, and this step has no **Keep** button — **Back** is what drops the
    pending question without saving. So when there's an existing answer, show
    it and say plainly that a reply overwrites it.
    """
    e = discord.Embed(title=question_field_name(question.prompt), color=embed_color)
    if existing:
        e.add_field(
            name="Current answer",
            value=existing[:_FIELD_VALUE_LIMIT],
            inline=False,
        )
        e.description = (
            "✏️ **Whatever you send next replaces the answer above** — it isn't "
            "added to it. Tap **Back** to leave it unchanged and pick a "
            "different question."
        )
    else:
        e.description = (
            "Reply with your answer, or use **Back** to pick a different question."
        )
    e.set_footer(text=f"Icebreaker {answered_count + 1} of up to {target_questions}")
    return e


__all__ = [
    "build_bio_embed",
    "build_field_prompt_embed",
    "build_question_prompt_embed",
    "FieldSnapshot",
    "QuestionSnapshot",
]
