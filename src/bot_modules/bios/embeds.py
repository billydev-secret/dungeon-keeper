"""Embed builder for the bios cog.

Pure: takes a fully-snapshotted `BioRenderPayload` and returns a
`discord.Embed`. No template lookups, no Discord state — what the user
entered at submit time is exactly what renders, even if the template
or question pool changed afterward.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot_modules.bios.logic import (
    BioRenderPayload,
    FieldSnapshot,
    QuestionSnapshot,
    cap_field_values_for_embed,
    cap_question_answers_for_embed,
    shrink_to_embed_total,
)


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
        embed.add_field(name=snap.label, value=snap.value, inline=inline)

    for snap in questions:
        if snap.skipped or not snap.answer:
            continue
        embed.add_field(
            name=f"› {snap.question_text}",
            value=snap.answer,
            inline=False,
        )

    ts = _parse_timestamp(payload.created_at_iso)
    if ts is not None:
        embed.timestamp = ts
    return embed


__all__ = ["build_bio_embed", "FieldSnapshot", "QuestionSnapshot"]
