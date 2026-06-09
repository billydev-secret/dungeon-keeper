"""Pure helpers for the emoji-stealer cog.

Carries the validation, URL building, and text-extraction logic that used
to live alongside the Discord glue in ``cogs/emoji_stealer_cog.py``.
Every function here is unit-testable without a real Discord client.
"""

from __future__ import annotations

import re

DISCORD_MAX_EMOJI_BYTES = 256 * 1024


_EMOJI_RE = re.compile(r"<(a?):(\w+):(\d+)>")
_HTTPS_RE = re.compile(r"^https://", re.IGNORECASE)


def emoji_cdn_url(emoji_id: int, animated: bool) -> str:
    """Build the Discord CDN URL for a custom emoji by id + animated flag."""
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"


def sanitize_emoji_name(name: str) -> str:
    """Normalize a user-supplied emoji name to Discord's allowed character set.

    Replaces any non-alphanumeric (other than ``_``) with ``_``, caps the
    length at 32 chars, and pads single-character names with ``_e`` to
    satisfy Discord's 2-char minimum.
    """
    name = re.sub(r"[^\w]", "_", name)[:32]
    return name if len(name) >= 2 else name + "_e"


def looks_like_image(data: bytes) -> bool:
    """Return True if ``data`` begins with PNG, JPEG, GIF, or WEBP magic bytes.

    Mirrors the image types Discord accepts for custom emoji, so the cog can
    reject non-image payloads (an HTML error page, or a message-link body
    served as 200 OK) with a friendly message before handing them to
    discord.py — which would otherwise raise a bare ``ValueError`` that the
    cog's exception handlers don't catch.
    """
    if len(data) < 12:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data[:3] == b"GIF":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def is_https_url(url: str) -> bool:
    """Return True if ``url`` starts with ``https://`` (case-insensitive).

    The cog refuses ``http://`` URLs both because Discord's CDN serves only
    HTTPS and because the bot host may block plain-HTTP outbound traffic.
    """
    return bool(_HTTPS_RE.match(url))


def validate_emoji_name(name: str) -> tuple[bool, str, str | None]:
    """Validate and sanitize an emoji name.

    Returns ``(ok, cleaned_name, error_message)``. ``cleaned_name`` is the
    sanitized form; ``error_message`` is the user-facing string when the
    name fails validation, else None.
    """
    clean = sanitize_emoji_name(name)
    if len(clean) < 2:
        return (
            False,
            clean,
            "Emoji name must be at least 2 characters (letters, numbers, underscores).",
        )
    return True, clean, None


def extract_emojis_from_text(content: str) -> list[tuple[bool, str, int]]:
    """Parse ``<a?:name:id>`` references out of message text.

    Returns a list of ``(animated, name, emoji_id)`` tuples in the order
    they first appear, deduplicated by emoji_id so a message that mentions
    the same emoji twice only yields one entry.
    """
    seen: set[int] = set()
    out: list[tuple[bool, str, int]] = []
    for animated_str, name, id_str in _EMOJI_RE.findall(content or ""):
        emoji_id = int(id_str)
        if emoji_id in seen:
            continue
        seen.add(emoji_id)
        out.append((bool(animated_str), name, emoji_id))
    return out


def build_steal_prompt(
    *, n_emoji: int, guild_count: int, first_emoji_name: str, first_guild_name: str
) -> str:
    """Build the prompt text for the message-context steal picker.

    Mirrors the cog's old inline branching: multiple emojis × multiple
    guilds gets one prompt, multiple emojis but one guild gets another,
    and a single emoji gets the final form. Extracted so the wording can
    be tested without spinning up an interaction.
    """
    if n_emoji > 1 and guild_count > 1:
        return f"Found **{n_emoji}** emojis — pick one and a server:"
    if n_emoji > 1:
        return f"Found **{n_emoji}** emojis — pick one to add to **{first_guild_name}**:"
    return f"Add `:{first_emoji_name}:` — which server?"


def format_steal_all_summary(
    *,
    added_mentions: list[str],
    guild_name: str,
    failed: list[tuple[str, str]],
) -> str:
    """Format the message that follows a Steal-All button click.

    Combines the count + emoji list for successes with a ``name (reason)``
    list for failures. Lives here so a future caller (queue worker,
    dashboard, etc.) can reuse the same wording.
    """
    lines: list[str] = []
    if added_mentions:
        count = len(added_mentions)
        plural = "s" if count != 1 else ""
        emoji_str = " ".join(added_mentions)
        lines.append(
            f"Added **{count}** emoji{plural} to **{guild_name}**: {emoji_str}"
        )
    if failed:
        fail_str = ", ".join(f"`:{n}:` ({r})" for n, r in failed)
        lines.append(f"Failed **{len(failed)}**: {fail_str}")
    return "\n".join(lines)
