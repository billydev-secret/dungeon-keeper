"""Pure decision and formatting logic for the DM-permission cog.

Everything here takes plain Python primitives and returns plain values
or formatted strings — no DB access, no Discord network calls. The cog
keeps the async glue (DB IO, message sends, view registration) and
delegates these decisions and string builds to this module.

Companion to ``bot_modules/services/dm_perms_service.py`` (DB layer):
do not move CRUD here; do not move pure logic into the service. That
split keeps each module's coverage and test surface obvious.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

import discord

# The service module already owns the canonical request-type vocabulary
# and ``resolve_mode``. We re-export ``request_type_label`` through this
# module's call chain rather than importing it here so the dependency
# direction stays one-way: cog → logic → service.


def safe_field_text(text: str | None) -> str:
    """Escape markdown in user-supplied text before placing it into an embed.

    DM request reasons come from a Discord modal — without escaping, a
    requester could craft markdown links / formatting that the recipient
    sees as if the bot itself authored them. Empty values render as an
    em-dash so the embed field isn't blank (Discord drops blank fields).
    """
    if not text:
        return "—"
    return discord.utils.escape_markdown(text)


def clamp_reason(reason: str, max_len: int) -> str:
    """Truncate ``reason`` to ``max_len`` characters with a trailing ellipsis.

    Used as defence-in-depth for callers that bypass the request modal
    (which already enforces its own ``max_length``). Leaves anything at
    or under the limit untouched.
    """
    if len(reason) > max_len:
        return reason[: max_len - 1] + "…"
    return reason


def classify_dm_request(
    *,
    target_in_guild: bool,
    is_self: bool,
    target_is_bot: bool,
    target_mode: str,
    is_mutual: bool,
    has_pending: bool,
    target_display_name: str,
) -> Optional[str]:
    """Validate a pending DM request and return an error message, or None.

    Cases (in priority order — first match wins):

    - ``target_in_guild=False``: target is a ``User`` not a ``Member`` (left
      the guild or never joined) → can't check DM mode, refuse.
    - ``is_self=True``: requester targeted themselves.
    - ``target_is_bot=True``: bots don't accept DM requests.
    - ``target_mode == "closed"``: target opted out.
    - ``target_mode == "open"``: no request needed — they accept DMs from
      anyone. The cog still surfaces this so the user knows to just DM.
    - ``is_mutual=True``: there's already an accepted consent pair.
    - ``has_pending=True``: a pending request already exists for this pair.
    - otherwise: returns None — the cog should proceed with the request.

    Takes primitives rather than discord objects so the function is fully
    unit-testable. ``target_display_name`` only feeds the error strings.
    """
    if not target_in_guild:
        return (
            "❌ I couldn't check that user's DM preference — they may not be in this server."
        )
    if is_self:
        return "❌ You can't send a request to yourself!"
    if target_is_bot:
        return "❌ Bots don't accept DM requests."
    if target_mode == "closed":
        return f"❌ {target_display_name} isn't accepting DM requests right now."
    if target_mode == "open":
        return (
            f"❌ {target_display_name} has their DMs open — no request needed, just message them!"
        )
    if is_mutual:
        return "❌ You two already have a connection — no need to request again."
    if has_pending:
        return "❌ You already have a pending request to them — wait for them to respond."
    return None


def dm_status_text(mutual: bool) -> str:
    """Return the one-line status string for ``/dm_status``.

    Trivial mapping, but extracted so the wording is testable and lives
    next to the rest of the DM-perm copy.
    """
    return "✅ You two are connected." if mutual else "❌ No connection yet."


def pick_dm_roles_to_remove(roles: Iterable[discord.Role]) -> list[discord.Role]:
    """Choose which DM-mode roles to strip when a member has multiple.

    Only one of (Open / Ask / Closed) should be active. When more than
    one is present (e.g. caused by a race during ``/dm_set_mode`` retries
    or by the web UI), keep the role with the highest position and return
    the others for removal. Returns an empty list when 0 or 1 DM roles
    are present.

    ``roles`` is the (already-filtered) list of DM-mode roles found on a
    member. The cog passes a list filtered against ``DM_ROLE_NAMES`` so
    this function stays oblivious to role names.
    """
    role_list = list(roles)
    if len(role_list) <= 1:
        return []
    keep = max(role_list, key=lambda r: r.position)
    return [r for r in role_list if r is not keep]


# ── Audit-log line builders ──────────────────────────────────────────


def audit_line_asked(requester_name: str, target_name: str, type_label: str) -> str:
    """Audit feed line emitted when a DM request is first sent."""
    return f"DM request asked: {requester_name} ➝ {target_name} ({type_label})"


def audit_line_accepted(requester_name: str, target_name: str, type_label: str) -> str:
    """Audit feed line emitted when a DM request is accepted."""
    return f"DM request accepted: {requester_name} ↔ {target_name} ({type_label})"


def audit_line_denied(requester_name: str, target_name: str, type_label: str) -> str:
    """Audit feed line emitted when a DM request is denied."""
    return f"DM request denied: {requester_name} ➝ {target_name} ({type_label})"


def audit_line_expired(requester_name: str, target_name: str, type_label: str) -> str:
    """Audit feed line emitted when a DM request times out unanswered."""
    return f"DM request expired: {requester_name} ➝ {target_name} ({type_label})"


def audit_line_revoked(
    requester_name: str, target_name: str, actor_name: str
) -> str:
    """Audit feed line emitted when a consent pair is revoked.

    ``actor_name`` is who triggered the revoke (either side of the pair
    can do so). It's repeated even when it equals one of the user names
    so the audit-feed reader can see who pressed the button.
    """
    return (
        f"DM permission revoked: {requester_name} ↔ {target_name} (by {actor_name})"
    )


def display_name_for(member: Optional[discord.Member], fallback_id: int) -> str:
    """Return ``member.display_name`` if present, else the bare id string.

    The cog reaches for this whenever a member may have left the guild
    between an audit event being scheduled and being rendered (the
    expiry sweep is the canonical case).
    """
    return member.display_name if member is not None else str(fallback_id)
