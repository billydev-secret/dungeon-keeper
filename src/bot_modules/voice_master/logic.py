"""Pure decision and formatting logic for the Voice Master cog.

Everything here takes plain Python primitives and returns plain values or
small dataclasses — no DB access, no Discord network calls, no Discord
object construction. The cog keeps the async glue (DB IO, channel.create/
edit/delete, View/Modal classes, voice-state listeners) and delegates
these decisions and string builds to this module.

Companion to ``bot_modules/services/voice_master_service.py`` (DB layer):
that module owns the canonical row dataclasses and the name-resolution /
reconciliation pure helpers already. Do not move CRUD here; do not move
duplicate pure logic here either — re-use it from the service.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from bot_modules.services.voice_master_service import (
    ACCESS_LOCKED,
    ACCESS_NSFW,
    ACCESS_OPEN,
    ACCESS_SPECTATE,
)


# ---------------------------------------------------------------------------
# Claim eligibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimDecision:
    """Outcome of evaluating a ``/voice claim`` attempt.

    Exactly one of ``eligible``/``error_message`` is meaningful per call:

    - ``eligible=True``: caller may proceed to grant ownership.
    - ``eligible=False`` with ``retry_seconds is not None``: owner is still
      within the grace period; show ``error_message`` and surface the
      countdown to the requester.
    - ``eligible=False`` with ``retry_seconds is None``: definite reject —
      show ``error_message`` only.
    """
    eligible: bool
    retry_seconds: int | None
    error_message: str | None


def classify_claim_attempt(
    *,
    owner_present: bool,
    owner_left_at: float | None,
    now: float,
    owner_grace_s: int,
    caller_is_owner: bool,
) -> ClaimDecision:
    """Decide whether ``/voice claim`` should grant ownership.

    Priority order — first match wins:

    - ``caller_is_owner`` → reject (no-op).
    - Owner left the server (``owner_present=False``) → eligible.
    - Owner left the channel ≥ ``owner_grace_s`` ago → eligible.
    - Owner left the channel but the grace window hasn't elapsed → reject
      with ``retry_seconds`` populated so the caller can wait it out.
    - Otherwise (owner present and active in/around the channel) → reject.
    """
    if caller_is_owner:
        return ClaimDecision(
            eligible=False,
            retry_seconds=None,
            error_message="You already own this channel.",
        )
    if not owner_present:
        return ClaimDecision(eligible=True, retry_seconds=None, error_message=None)
    if owner_left_at is not None:
        elapsed = now - owner_left_at
        if elapsed >= owner_grace_s:
            return ClaimDecision(
                eligible=True, retry_seconds=None, error_message=None
            )
        wait = max(int(owner_grace_s - elapsed), 0)
        return ClaimDecision(
            eligible=False,
            retry_seconds=wait,
            error_message=(
                f"The owner left {int(elapsed)}s ago — "
                f"claim available in {wait}s."
            ),
        )
    return ClaimDecision(
        eligible=False,
        retry_seconds=None,
        error_message="The owner is still active in or watching the channel.",
    )


# ---------------------------------------------------------------------------
# Trust / block list validation
# ---------------------------------------------------------------------------


def validate_trust_add(
    *,
    target_is_bot: bool,
    target_is_self: bool,
    disable_saves: bool,
    saveable_fields: Iterable[str],
) -> str | None:
    """Validate a ``/voice trusted add`` attempt; return an error or None.

    The cog already gates ``interaction.guild is None`` upstream; this only
    covers the policy + identity checks. Order matters — bot/self errors
    are shown even when saves are disabled, because they're definite even
    in the alternate world where saves get re-enabled.
    """
    if target_is_bot:
        return "Can't trust bots."
    if target_is_self:
        return "You're always trusted by yourself."
    if disable_saves or "trusted" not in set(saveable_fields):
        return (
            "Saving the trust list is disabled by an admin on this server."
        )
    return None


def validate_block_add(
    *,
    target_is_bot: bool,
    target_is_self: bool,
    disable_saves: bool,
    saveable_fields: Iterable[str],
) -> str | None:
    """Validate a ``/voice blocked add`` attempt; return an error or None.

    Mirror of ``validate_trust_add`` but with blocklist-specific wording
    (you can't block yourself; trusting yourself is a no-op).
    """
    if target_is_bot:
        return "Can't block bots."
    if target_is_self:
        return "Can't block yourself."
    if disable_saves or "blocked" not in set(saveable_fields):
        return (
            "Saving the blocklist is disabled by an admin on this server."
        )
    return None


def format_trust_add_result(
    *,
    target_mention: str,
    added: bool,
    evicted_id: int | None,
) -> str:
    """Build the reply for ``/voice trusted add`` after a DB write.

    ``added=False`` means the target was already present (idempotent).
    ``evicted_id`` is the oldest entry kicked out when the cap is hit.
    """
    if not added:
        return f"{target_mention} is already on your trust list."
    msg = f"Added {target_mention} to your trust list."
    if evicted_id is not None:
        msg += f" (Cap reached — removed <@{evicted_id}>.)"
    return msg


def format_block_add_result(
    *,
    target_mention: str,
    added: bool,
    evicted_id: int | None,
) -> str:
    """Mirror of ``format_trust_add_result`` for the blocklist."""
    if not added:
        return f"{target_mention} is already on your blocklist."
    msg = f"Added {target_mention} to your blocklist."
    if evicted_id is not None:
        msg += f" (Cap reached — removed <@{evicted_id}>.)"
    return msg


def format_trusted_list(ids: list[int]) -> str:
    """Render the ``/voice trusted list`` reply (one ephemeral line).

    Empty list returns the dedicated empty-string message rather than a
    blank ``Trusted (0):`` so the user knows the system worked.
    """
    if not ids:
        return "Your trust list is empty."
    rendered = ", ".join(f"<@{uid}>" for uid in ids)
    return f"Trusted ({len(ids)}): {rendered}"


def format_blocked_list(ids: list[int]) -> str:
    """Mirror of ``format_trusted_list`` for the blocklist."""
    if not ids:
        return "Your blocklist is empty."
    rendered = ", ".join(f"<@{uid}>" for uid in ids)
    return f"Blocked ({len(ids)}): {rendered}"


# ---------------------------------------------------------------------------
# Hub-join planning
# ---------------------------------------------------------------------------


# Voice/text permissions that separate a "speaker" from a "spectator". In
# spectator mode these are denied on the audience target (``@everyone`` when
# ungated, or the gate role when gated) and granted explicitly to whoever may
# participate — a member overwrite outranks the role/@everyone deny, so the
# owner, trusted, invited and already-present members keep full rights. Denying
# ``stream`` covers both webcam and Go Live (Discord's "Video" permission);
# denying the two ``send_messages`` perms makes the side chat read-only.
SPECTATOR_PARTICIPATION_PERMS: tuple[str, ...] = (
    "speak",
    "stream",
    "send_messages",
    "send_messages_in_threads",
)


@dataclass(frozen=True)
class OverwritePlanEntry:
    """One line of the channel-create overwrite plan.

    ``target_id`` is the snowflake (member or role). ``target_kind`` is
    ``"everyone"`` for the default-role row, ``"owner"`` for the channel
    owner, ``"trusted"``/``"blocked"`` for trust/block entries, or
    ``"gate_role"`` for the spectator gate role. The cog maps these back to
    actual ``discord.Role``/``discord.Member`` objects.

    ``view_channel``/``connect`` and the spectator participation fields
    (``speak``/``stream``/``send_messages``/``send_messages_in_threads``):
    ``True`` grants, ``False`` denies, ``None`` inherits (matches
    ``PermissionOverwrite`` semantics).
    """
    target_id: int
    target_kind: str
    view_channel: bool | None
    connect: bool | None
    speak: bool | None = None
    stream: bool | None = None
    send_messages: bool | None = None
    send_messages_in_threads: bool | None = None


@dataclass(frozen=True)
class OverwritePlan:
    """The complete overwrite plan plus dropped trust/block ids.

    ``missing_target_ids`` is the trust/block ids the cog passed in that
    no longer correspond to guild members — surfaced to the owner as a
    skipped-targets DM note.
    """
    entries: list[OverwritePlanEntry]
    missing_target_ids: list[int]


def plan_initial_overwrites(
    *,
    owner_id: int,
    everyone_role_id: int,
    profile_locked: bool,
    profile_hidden: bool,
    profile_spectator: bool = False,
    gate_role_id: int | None = None,
    trusted_ids: list[int],
    blocked_ids: list[int],
    present_member_ids: set[int],
) -> OverwritePlan:
    """Build an overwrite plan for a freshly-created Voice Master channel.

    ``present_member_ids`` is the guild's current member roster (or any
    superset). Trust/block ids not in this set are returned in the plan's
    ``missing_target_ids`` list and omitted from the entries.

    Spectator mode (``profile_spectator``) and lock are mutually exclusive at
    the caller; if both arrive, spectator wins. When ``gate_role_id`` is set
    (non-zero), spectating is gated to that role: ``@everyone`` is denied
    Connect (visible + readable, but can't join) and the gate role becomes the
    muted/no-video/read-only audience. Ungated, ``@everyone`` itself is the
    audience. The owner and trusted members get explicit participation grants
    so the audience deny never reaches them.
    """
    gated = profile_spectator and bool(gate_role_id)
    entries: list[OverwritePlanEntry] = []

    everyone_connect: bool | None = None
    everyone_speak: bool | None = None
    everyone_stream: bool | None = None
    everyone_send: bool | None = None
    everyone_send_threads: bool | None = None
    if profile_spectator:
        if gated:
            # Block joining only — role-less members still see and read.
            everyone_connect = False
        else:
            everyone_speak = False
            everyone_stream = False
            everyone_send = False
            everyone_send_threads = False
    elif profile_locked:
        everyone_connect = False
    entries.append(
        OverwritePlanEntry(
            target_id=everyone_role_id,
            target_kind="everyone",
            view_channel=False if profile_hidden else None,
            connect=everyone_connect,
            speak=everyone_speak,
            stream=everyone_stream,
            send_messages=everyone_send,
            send_messages_in_threads=everyone_send_threads,
        )
    )
    if gated:
        entries.append(
            OverwritePlanEntry(
                target_id=int(gate_role_id),  # type: ignore[arg-type]
                target_kind="gate_role",
                view_channel=None,
                connect=True,
                speak=False,
                stream=False,
                send_messages=False,
                send_messages_in_threads=False,
            )
        )
    speaker = _speaker_grant_fields(profile_spectator)
    entries.append(
        OverwritePlanEntry(
            target_id=owner_id,
            target_kind="owner",
            view_channel=True,
            connect=True,
            **speaker,
        )
    )
    missing: list[int] = []
    for uid in trusted_ids:
        if uid not in present_member_ids:
            missing.append(uid)
            continue
        entries.append(
            OverwritePlanEntry(
                target_id=uid,
                target_kind="trusted",
                view_channel=True,
                connect=True,
                **speaker,
            )
        )
    for uid in blocked_ids:
        if uid not in present_member_ids:
            missing.append(uid)
            continue
        entries.append(
            OverwritePlanEntry(
                target_id=uid,
                target_kind="blocked",
                view_channel=None,
                connect=False,
            )
        )
    return OverwritePlan(entries=entries, missing_target_ids=missing)


def _speaker_grant_fields(spectator: bool) -> dict[str, bool | None]:
    """Participation-grant kwargs for a privileged member's plan entry.

    Only meaningful in spectator mode (where the audience deny would otherwise
    mute them); ``None`` everywhere else so open/lock-mode shapes are unchanged.
    """
    value: bool | None = True if spectator else None
    return {perm: value for perm in SPECTATOR_PARTICIPATION_PERMS}


def plan_lock_text_grants(
    *, present_member_ids: list[int], owner_id: int, bot_id: int | None = None
) -> list[int]:
    """Member ids that need an explicit ``connect`` grant when locking.

    Discord gates a voice channel's integrated text chat behind the
    ``Connect`` permission, so the lock (denying ``Connect`` to ``@everyone``)
    also strips text-chat access from everyone in the channel who relied on
    ``@everyone``. To keep the chat usable for the people already inside, each
    present member is given an explicit ``connect=True`` overwrite. The owner
    already has a persistent overwrite and the bot needs no grant, so both are
    skipped. Input order is preserved and duplicates collapsed.
    """
    skip = {owner_id}
    if bot_id is not None:
        skip.add(bot_id)
    seen: set[int] = set()
    out: list[int] = []
    for uid in present_member_ids:
        if uid in skip or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def plan_unlock_overwrite_cleanup(
    *,
    member_overwrites: list[tuple[int, bool | None, bool | None]],
    owner_id: int,
    trusted_ids: list[int],
    blocked_ids: list[int],
) -> list[int]:
    """Member ids whose per-member overwrite should be cleared on unlock.

    Locking grants transient ``connect=True`` overwrites — with ``view_channel``
    left untouched — to whoever was in the channel (see
    :func:`plan_lock_text_grants`); unlock drops exactly those again so the
    channel returns to a clean state.

    Each entry in ``member_overwrites`` is ``(member_id, connect, view_channel)``
    read from the live overwrite. Only the lock-grant *shape* is removed:
    ``connect is True`` **and** ``view_channel is None``. Every other grant the
    bot writes sets ``view_channel=True`` (invite, knock-accept, claim,
    transfer, the owner's own grant) or ``connect=False`` (block), so none of
    those match and a one-off invited guest keeps their access. The owner and
    any trusted/blocked ids are excluded as a belt-and-suspenders guard. Input
    order preserved.
    """
    keep = {owner_id, *trusted_ids, *blocked_ids}
    return [
        mid
        for (mid, connect, view_channel) in member_overwrites
        if mid not in keep and connect is True and view_channel is None
    ]


def plan_hide_text_grants(
    *, present_member_ids: list[int], owner_id: int, bot_id: int | None = None
) -> list[int]:
    """Member ids that need an explicit ``view_channel`` grant when hiding.

    The mirror of :func:`plan_lock_text_grants` for the hide toggle. Discord
    gates a voice channel's integrated text chat behind ``View Channel`` (the
    permission "includes reading messages ... and joining voice channels"), so
    hiding — denying ``View Channel`` to ``@everyone`` — also strips the side
    chat from everyone inside who relied on ``@everyone``. To keep the chat
    usable for the people already present, each is given an explicit
    ``view_channel=True`` overwrite. The owner already has a persistent grant
    and the bot needs none, so both are skipped. A channel that is both hidden
    and locked needs this grant *and* the lock's Connect grant; the two compose
    on the same member overwrite. Input order preserved, duplicates collapsed.
    """
    skip = {owner_id}
    if bot_id is not None:
        skip.add(bot_id)
    seen: set[int] = set()
    out: list[int] = []
    for uid in present_member_ids:
        if uid in skip or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def plan_unhide_view_cleanup(
    *,
    member_overwrites: list[tuple[int, bool | None]],
    owner_id: int,
    trusted_ids: list[int],
    blocked_ids: list[int],
) -> list[int]:
    """Member ids whose transient ``view_channel`` grant should be reset on unhide.

    Hiding grants ``view_channel=True`` to whoever was inside (see
    :func:`plan_hide_text_grants`); unhide clears that field again so no stale
    grant survives to reveal the channel the next time it's hidden. Each entry
    is ``(member_id, view_channel)`` read from the live overwrite. Any
    non-privileged member carrying ``view_channel is True`` is returned; the
    caller resets *only* the ``view_channel`` field and drops the overwrite if
    nothing else remains, so a member also rescued by the lock keeps their
    ``connect`` grant. Owner and trusted/blocked ids are excluded as a guard.

    Cleanup is field-scoped, not shape-matched: a one-off invited guest who is
    present at unhide also has their (now redundant) view grant trimmed. That is
    harmless — unhiding restores ``@everyone`` visibility — and keeps the pass
    leak-free, which matters more here than for unlock since a lingering
    ``view_channel`` is exactly what hide exists to prevent. Input order
    preserved.
    """
    keep = {owner_id, *trusted_ids, *blocked_ids}
    return [
        mid
        for (mid, view_channel) in member_overwrites
        if mid not in keep and view_channel is True
    ]


def plan_spectator_speaker_grants(
    *, present_member_ids: list[int], owner_id: int, bot_id: int | None = None
) -> list[int]:
    """Member ids that need a transient speaker grant when spectator turns on.

    Anyone already in the channel when the owner enables spectator mode keeps
    full participation (the design's "anyone already in + invited" rule). They
    joined via ``@everyone`` and have no persistent overwrite, so without an
    explicit grant the audience deny would mute them. The owner already has a
    persistent overwrite (granted the speaker fields directly) and the bot
    needs nothing, so both are skipped. Order preserved, duplicates collapsed.
    """
    skip = {owner_id}
    if bot_id is not None:
        skip.add(bot_id)
    seen: set[int] = set()
    out: list[int] = []
    for uid in present_member_ids:
        if uid in skip or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def plan_spectator_grant_cleanup(
    *,
    member_overwrites: list[tuple[int, bool | None, bool | None, bool | None]],
    owner_id: int,
    trusted_ids: list[int],
    blocked_ids: list[int],
) -> list[int]:
    """Member ids whose *transient* spectator speaker grant to drop on disable.

    Enabling spectator grants ``speak=True`` (plus the other participation
    perms) to the already-present, otherwise-unprivileged members
    (:func:`plan_spectator_speaker_grants`); disabling drops exactly those.

    Each entry is ``(member_id, speak, connect, view_channel)`` read from the
    live overwrite. The transient grant's shape is ``speak is True`` **and**
    ``connect is None`` **and** ``view_channel is None`` — i.e. a pure
    participation grant with no persistent access. Owner/trusted/invited
    speakers instead carry ``connect=True``/``view_channel=True``, so they're
    handled separately (their participation fields are reset in place, not
    removed). Owner/trusted/blocked ids are excluded as a guard. Order kept.
    """
    keep = {owner_id, *trusted_ids, *blocked_ids}
    return [
        mid
        for (mid, speak, connect, view_channel) in member_overwrites
        if mid not in keep
        and speak is True
        and connect is None
        and view_channel is None
    ]


def classify_access_mode(
    *,
    everyone_connect: bool | None,
    everyone_speak: bool | None,
    gate_role_set: bool,
    gate_role_connect: bool | None,
    gate_role_speak: bool | None,
) -> str:
    """Classify a channel's current access mode from its overwrite values.

    Returns ``"open"``, ``"lock"``, or ``"spectate"``. The gate-role check
    comes first because gated-spectator denies Connect to ``@everyone`` — the
    same shape as a plain lock — so the two are only distinguishable by the
    gate role carrying ``connect=True`` + a participation deny.
    """
    if (
        gate_role_set
        and gate_role_connect is True
        and gate_role_speak is False
    ):
        return "spectate"
    if everyone_speak is False:
        return "spectate"
    if everyone_connect is False:
        return "lock"
    return "open"


def select_effective_limit(
    *, saved_limit: int, default_user_limit: int
) -> int:
    """Pick the user-limit to apply to a new channel.

    Profile-saved limit wins when set (>0). Otherwise the per-guild
    default. Returning 0 means "no cap", which the cog translates by
    omitting ``user_limit`` from the create kwargs.
    """
    if saved_limit > 0:
        return saved_limit
    return default_user_limit


def select_effective_bitrate(
    *, saved_bitrate: int | None, default_bitrate: int, guild_max_bitrate: int
) -> int:
    """Pick the bitrate to apply, clamped to the guild's tier maximum.

    A saved per-user bitrate wins, else the guild-configured default. When
    neither is set (both ``0``/``None``) we fall back to the highest bitrate the
    guild's boost tier allows rather than Discord's base default. The result is
    always clamped to ``guild_max_bitrate`` so a value saved under a higher
    boost tier can't 400 the channel create after a downgrade.
    """
    chosen = saved_bitrate or default_bitrate or guild_max_bitrate
    if guild_max_bitrate > 0:
        chosen = min(chosen, guild_max_bitrate)
    return chosen


def style_lease_blocks(
    *, economy_enabled: bool, price: int, entitled: bool
) -> bool:
    """Whether the voice-style lease blocks rename/limit for this member.

    Economy sinks round 3, stage 3 (spec §6): rename and user-limit are leased
    controls, but the paywall arms only while the guild's economy is enabled
    AND ``price_voice_style`` is above zero — price 0 is the shipped-dark
    default where every member keeps the controls free, and it doubles as the
    per-guild opt-out. An armed paywall passes members entitled to the
    ``voice_style`` perk (beneficiary-based, so a gifted lease counts). The
    access dial, invite/kick/transfer, and reset are never gated.
    """
    return economy_enabled and price > 0 and not entitled


def build_skipped_payload(
    *, name_fell_back: bool, missing_target_count: int
) -> list[str]:
    """Build the ``applied_skipped`` payload for the channel-create audit row.

    Tokens, not human strings — the audit reader knows the vocabulary
    (``"name"`` = the saved name was filtered; ``"missing_members"`` =
    trust/block ids no longer in the server). Order is stable for tests.
    """
    out: list[str] = []
    if name_fell_back:
        out.append("name")
    if missing_target_count > 0:
        out.append("missing_members")
    return out


def build_hub_join_notes(
    *, name_fell_back: bool, fallback_name: str, missing_target_count: int
) -> str | None:
    """Build the post-create DM text owner gets when corners had to be cut.

    Returns ``None`` when nothing needs reporting so the cog can skip the
    DM entirely. The two notes are joined with newlines.
    """
    notes: list[str] = []
    if name_fell_back:
        notes.append(
            "Saved name was blocked by an admin filter — "
            f"using `{fallback_name}` instead."
        )
    if missing_target_count > 0:
        notes.append(
            f"{missing_target_count} member(s) on your trust/block list "
            "are no longer in this server and were skipped."
        )
    if not notes:
        return None
    return "\n".join(notes)


# ---------------------------------------------------------------------------
# Cooldown / cap checks
# ---------------------------------------------------------------------------


def hub_create_blocked_by_cooldown(
    *, now: float, last_create_at: float, cooldown_s: int
) -> bool:
    """True when a hub-join should be silently bounced for cooldown reasons.

    ``cooldown_s <= 0`` disables the check (no cooldown). The cog reads
    ``last_create_at`` from its in-memory dict, defaulted to 0.0.
    """
    if cooldown_s <= 0:
        return False
    return (now - last_create_at) < cooldown_s


# ---------------------------------------------------------------------------
# Profile reset
# ---------------------------------------------------------------------------


# What ``/voice profile reset`` accepts as ``field``.
PROFILE_RESET_FIELDS: frozenset[str] = frozenset(
    {"all", "name", "limit", "locked", "hidden", "spectator", "trusted", "blocked"}
)


def profile_reset_summary(field: str) -> str:
    """User-visible summary line for ``/voice profile reset``.

    The cog already validates ``field`` against the choice list; this
    function falls back to a generic message for any unknown value rather
    than raising so a forgotten value doesn't crash the command.
    """
    if field == "all":
        return "Saved profile, trust list, and blocklist cleared."
    if field == "trusted":
        return "Trust list cleared."
    if field == "blocked":
        return "Blocklist cleared."
    if field in {"name", "limit", "locked", "hidden"}:
        return f"`{field}` cleared from your saved profile."
    return f"`{field}` cleared from your saved profile."


# ---------------------------------------------------------------------------
# Admin audit summary lines
# ---------------------------------------------------------------------------


def build_force_delete_summary(
    *, channel_name: str, channel_id: int, owner_id: int
) -> str:
    """Build the mod-log body for a web admin force-delete."""
    return (
        f"Deleted channel `{channel_name}` (id `{channel_id}`) "
        f"owned by <@{owner_id}>."
    )


def build_force_transfer_summary(
    *,
    channel_name: str,
    channel_id: int,
    old_owner_id: int,
    new_owner_mention: str,
) -> str:
    """Build the mod-log body for a web admin force-transfer."""
    return (
        f"Channel `{channel_name}` (id `{channel_id}`): "
        f"<@{old_owner_id}> → {new_owner_mention}."
    )


def build_force_clear_profile_summary(*, target_mention: str) -> str:
    """Build the mod-log body for a web admin force-clear-profile."""
    return f"Cleared saved profile for {target_mention}."


# ---------------------------------------------------------------------------
# Command-time validation helpers (paired with voice_master_commands.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenameValidation:
    """Outcome of validating a rename submission.

    ``cleaned`` is the post-strip, post-truncate name to apply when
    ``error_message is None``. Otherwise ``error_message`` is what to
    show the user and ``cleaned`` is unspecified.
    """
    cleaned: str
    error_message: str | None


def validate_rename_input(
    raw_name: str,
    *,
    max_len: int,
    blocklist_patterns: list[str],
) -> RenameValidation:
    """Strip / cap / blocklist-check a proposed channel name.

    Order of checks mirrors ``_apply_rename``: trim → empty? → truncate →
    blocklist. Empty after strip yields the empty-name error; matching
    the server-wide blocklist yields the filtered-name error.
    """
    cleaned = raw_name.strip()
    if not cleaned:
        return RenameValidation(
            cleaned="", error_message="Channel name can't be empty."
        )
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    # name_is_blocked from the service lowercases the haystack itself, but
    # we replicate the semantics here so this helper has no service dep.
    needle = cleaned.lower()
    if any(p in needle for p in blocklist_patterns if p):
        return RenameValidation(
            cleaned=cleaned,
            error_message=(
                "That name matches a server-wide content filter — "
                "pick another."
            ),
        )
    return RenameValidation(cleaned=cleaned, error_message=None)


def validate_limit_value(value: int) -> str | None:
    """Validate a /limit input. ``None`` ⇒ accept; otherwise error to show."""
    if value < 0 or value > 99:
        return "User limit must be between 0 and 99 (0 = no cap)."
    return None


def parse_limit_input(raw: str) -> tuple[int | None, str | None]:
    """Parse the limit modal text input. Returns ``(value, error)``."""
    try:
        value = int(raw.strip())
    except ValueError:
        return None, "Limit must be a whole number."
    return value, None


def validate_transfer_target(
    *,
    target_is_bot: bool,
    target_is_current_owner: bool,
    target_in_channel: bool,
) -> str | None:
    """Validate ``/transfer`` target. ``None`` ⇒ accept; else error string."""
    if target_is_bot:
        return "Can't transfer ownership to a bot."
    if target_is_current_owner:
        return "You're already the owner."
    if not target_in_channel:
        return "The new owner must currently be in the voice channel."
    return None


def validate_invite_target(
    *,
    target_is_bot: bool,
    target_is_owner: bool,
) -> str | None:
    """Validate ``/invite`` target. ``None`` ⇒ accept; else error string."""
    if target_is_bot:
        return "Can't invite bots."
    if target_is_owner:
        return "You're already the owner."
    return None


def validate_kick_target(
    *,
    target_is_bot: bool,
    target_is_self_owner: bool,
) -> str | None:
    """Validate ``/kick`` target. ``None`` ⇒ accept; else error string."""
    if target_is_bot:
        return "Can't kick bots."
    if target_is_self_owner:
        return "You can't kick yourself — transfer ownership first."
    return None


def should_save_profile_field(
    *,
    saveable_key: str,
    disable_saves: bool,
    saveable_fields: Iterable[str],
) -> bool:
    """Decide whether a profile field should be persisted after an edit.

    Pure decision half of ``_maybe_save_profile_field`` — the commands
    layer still does the actual ``update_profile_field`` write.
    """
    if disable_saves:
        return False
    return saveable_key in set(saveable_fields)


# ---------------------------------------------------------------------------
# Command-time formatters
# ---------------------------------------------------------------------------


def format_edit_rate_limit_error(
    *, retry_seconds: float, window_s: float
) -> str:
    """Build the ephemeral reply when a channel edit hits the 2/window cap."""
    return (
        f"Discord limits voice channel edits to 2 per "
        f"{int(window_s / 60)} minutes — try again in {int(retry_seconds)}s."
    )


def format_lock_result(*, locked: bool) -> str:
    """Confirmation reply after a lock/unlock."""
    return f"Channel **{'locked' if locked else 'unlocked'}**."


def format_hide_result(*, hidden: bool) -> str:
    """Confirmation reply after a hide/unhide."""
    return f"Channel is now **{'hidden' if hidden else 'visible'}**."


def format_spectator_result(*, spectator: bool, gated: bool = False) -> str:
    """Confirmation reply after toggling spectator mode."""
    if not spectator:
        return "Spectator mode **off** — the channel is open again."
    if gated:
        return (
            "Spectator mode **on** (gated) — role-holders can join muted, with "
            "no camera, read-only in chat. Others can't join."
        )
    return (
        "Spectator mode **on** — anyone can join muted, with no camera, "
        "read-only in chat. Invite people to let them speak."
    )


def format_access_result(*, state: str, gated: bool = False) -> str:
    """Confirmation reply after setting the single access-state dial.

    ``gated`` only matters for the spectator state (whether a gate role narrows
    the audience).
    """
    if state == ACCESS_LOCKED:
        return (
            "Access set to **NSFW — locked**: age-gated, hidden from the list, "
            "and invite-only. People you invite can still see and join."
        )
    if state == ACCESS_SPECTATE:
        if gated:
            return (
                "Access set to **Spectator** (gated, age-gated): role-holders "
                "join muted, no camera, read-only in chat. Others can't join. "
                "Invite people to let them speak."
            )
        return (
            "Access set to **Spectator** (age-gated): anyone can join muted, no "
            "camera, read-only in chat. Invite people to let them speak."
        )
    if state == ACCESS_NSFW:
        return (
            "Access set to **NSFW — open**: age-gated, but anyone can still see "
            "and join."
        )
    return "Access set to **Open**: anyone can see and join, no age gate."


def format_rename_result(*, new_name: str) -> str:
    """Confirmation reply after a rename."""
    return f"Renamed to **{new_name}**."


def format_limit_result(*, new_limit: int) -> str:
    """Confirmation reply after setting the user limit.

    Treats ``new_limit <= 0`` as "no cap" wording.
    """
    return (
        f"User limit set to **{new_limit if new_limit > 0 else 'no cap'}**."
    )


def format_reset_result(*, also_profile: bool) -> str:
    """Confirmation reply after a reset (channel-only vs channel+profile)."""
    if also_profile:
        return "Channel **and** saved profile reset."
    return "Channel reset to defaults (your saved profile is unchanged)."


def format_transfer_result(*, new_owner_mention: str) -> str:
    """Confirmation reply after ownership transfer."""
    return f"Ownership transferred to {new_owner_mention}."


def format_invite_result(
    *,
    target_mention: str,
    remember: bool,
    cap_evicted_id: int | None,
) -> str:
    """Confirmation reply after ``/invite``.

    ``remember=True`` means the target was also pushed onto the owner's
    trust list. ``cap_evicted_id`` is the id of the older entry kicked
    out when the trust cap was hit (or ``None``).
    """
    extra = ""
    if cap_evicted_id is not None:
        extra = f" (Trust list cap reached — removed <@{cap_evicted_id}>.)"
    word = "remembered" if remember else "invited"
    return f"{target_mention} {word} for this channel.{extra}"


def format_kick_result(
    *,
    target_mention: str,
    remember: bool,
    cap_evicted_id: int | None,
) -> str:
    """Confirmation reply after ``/kick``.

    Mirror of ``format_invite_result``: ``remember=True`` means the
    target was also pushed onto the owner's blocklist.
    """
    extra = ""
    if cap_evicted_id is not None:
        extra = f" (Block list cap reached — removed <@{cap_evicted_id}>.)"
    word = "blocked permanently" if remember else "kicked"
    return f"{target_mention} {word}.{extra}"


def build_join_url(*, guild_id: int, channel_id: int) -> str:
    """Build the deep-link URL Discord uses for jumping to a channel."""
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def format_invite_dm(
    *,
    channel_name: str,
    inviter_mention: str,
    guild_name: str,
    join_url: str,
) -> str:
    """DM body sent to an invitee after ``/invite``."""
    return (
        f"You've been invited to **{channel_name}** by "
        f"{inviter_mention} in **{guild_name}**.\n"
        f"{join_url}"
    )


def format_knock_accepted_dm(*, channel_name: str, join_url: str) -> str:
    """DM body sent to a requester when their knock is accepted."""
    return (
        f"Your knock on **{channel_name}** was accepted. {join_url}"
    )


# ---------------------------------------------------------------------------
# Picker / UI option plans (pure data; the cog turns them into Discord objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PickerOption:
    """One row of a Discord select-menu options list.

    The cog converts these to ``discord.SelectOption`` instances; tests
    can pin the labels/values without a Discord client.
    """
    label: str
    value: str
    description: str


@dataclass(frozen=True)
class TransferPickerPlan:
    """Plan for the transfer ownership picker.

    ``options`` is empty when no eligible members are available — the cog
    then shows the disabled "No eligible members in the channel" stub.
    """
    options: list[PickerOption]
    has_options: bool


@dataclass(frozen=True)
class MemberInfo:
    """Pure-Python projection of a Discord member for picker building."""
    id: int
    display_name: str
    name: str
    is_bot: bool


def build_transfer_picker_plan(
    members: list[MemberInfo],
    *,
    owner_id: int,
    max_options: int = 25,
) -> TransferPickerPlan:
    """Build the option list for the transfer-ownership picker.

    Filters bots and the current owner; truncates to ``max_options``
    (Discord's hard ceiling). Returns ``has_options=False`` when the
    filtered list is empty so the cog can disable the select.
    """
    options: list[PickerOption] = []
    for m in members:
        if m.is_bot or m.id == owner_id:
            continue
        options.append(
            PickerOption(
                label=m.display_name,
                value=str(m.id),
                description=f"@{m.name}",
            )
        )
        if len(options) >= max_options:
            break
    return TransferPickerPlan(options=options, has_options=bool(options))


@dataclass(frozen=True)
class UserPickerLabels:
    """Strings for the invite/kick ``_UserPickerView``."""
    placeholder: str
    action_one: str
    action_two: str


def user_picker_labels(mode: str) -> UserPickerLabels:
    """Resolve placeholder + button labels for the user-picker view.

    ``mode`` is ``"invite"`` or ``"kick"``. Anything else falls back to
    invite-mode wording so a typo doesn't crash the cog.
    """
    if mode == "kick":
        return UserPickerLabels(
            placeholder="Pick a member to kick",
            action_one="Kick",
            action_two="Permanent block (remember)",
        )
    return UserPickerLabels(
        placeholder="Pick a member to invite",
        action_one="Invite",
        action_two="Trusted invite (remember)",
    )


# ---------------------------------------------------------------------------
# Panel button registry (pure data — what each button looks like)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PanelButtonMeta:
    """Pure description of one panel action — its dropdown label + emoji.

    ``description`` is an optional second line shown under the option in the
    select menu (used by the access-state picker, where each state needs a word
    of explanation); it is empty for the plain action buttons.
    """
    action: str
    label: str
    emoji: str
    description: str = ""


PANEL_BUTTON_ORDER: tuple[str, ...] = (
    ACCESS_OPEN, ACCESS_NSFW, ACCESS_LOCKED, ACCESS_SPECTATE,
    "rename", "limit", "invite", "kick",
    "transfer", "reset",
)


_PANEL_BUTTON_META: dict[str, PanelButtonMeta] = {
    ACCESS_OPEN: PanelButtonMeta(
        ACCESS_OPEN, "Open", "🔓", "Anyone can see and join."
    ),
    ACCESS_NSFW: PanelButtonMeta(
        ACCESS_NSFW, "NSFW — open", "🔞",
        "Age-gated, but anyone can see and join.",
    ),
    ACCESS_LOCKED: PanelButtonMeta(
        ACCESS_LOCKED, "NSFW — locked", "🔒",
        "Age-gated, hidden, invite-only.",
    ),
    ACCESS_SPECTATE: PanelButtonMeta(
        ACCESS_SPECTATE, "Spectator", "🎭",
        "Age-gated audience: join muted, read-only.",
    ),
    "rename":      PanelButtonMeta("rename",      "Rename",      "✏️"),
    "limit":       PanelButtonMeta("limit",       "Limit",       "🔢"),
    "invite":      PanelButtonMeta("invite",      "Invite",      "👋"),
    "kick":        PanelButtonMeta("kick",        "Kick",        "🚫"),
    "transfer":    PanelButtonMeta("transfer",    "Transfer",    "👑"),
    "reset":       PanelButtonMeta("reset",       "Reset",       "🧹"),
}


def panel_button_meta(action: str) -> PanelButtonMeta | None:
    """Look up a panel button's metadata by action key, or ``None``."""
    return _PANEL_BUTTON_META.get(action)


def all_panel_button_metas() -> list[PanelButtonMeta]:
    """Return every panel button meta in canonical order."""
    return [_PANEL_BUTTON_META[a] for a in PANEL_BUTTON_ORDER]


# ---------------------------------------------------------------------------
# Panel select groups (the actions split across two dropdown menus)
# ---------------------------------------------------------------------------
#
# The panel is presented as two grouped select menus rather than a wall of
# buttons. Each group's tuple is its in-menu display order; PANEL_GROUP_ORDER
# (the order the menus themselves appear) derives from the dict so the two
# can't drift. The grouping is partition-checked in tests against
# PANEL_BUTTON_ORDER so an action can never silently fall off the panel.

_GROUP_ACTIONS: dict[str, tuple[str, ...]] = {
    "access": (ACCESS_OPEN, ACCESS_NSFW, ACCESS_LOCKED, ACCESS_SPECTATE),
    "settings": ("rename", "limit", "reset"),
    "permissions": ("invite", "kick", "transfer"),
}

PANEL_GROUP_ORDER: tuple[str, ...] = tuple(_GROUP_ACTIONS)

_GROUP_PLACEHOLDER: dict[str, str] = {
    "access": "Set who can see and join",
    "settings": "Change channel settings",
    "permissions": "Change channel permissions",
}


def panel_group_placeholder(group: str) -> str:
    """Return the dropdown placeholder text for a panel select group."""
    return _GROUP_PLACEHOLDER[group]


def panel_metas_for_group(group: str) -> list[PanelButtonMeta]:
    """Return the panel metas in one select group, in display order."""
    return [_PANEL_BUTTON_META[a] for a in _GROUP_ACTIONS[group]]
