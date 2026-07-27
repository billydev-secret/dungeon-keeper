"""Pure inactive-sweep logic — no Discord API calls, no database access.

The auto-sweep is a destructive, hard-to-reverse mass role-strip, so the
decision of *who* gets swept lives here in a single, trivially unit-testable
function. The cog and the background loop both route through it, and tests pin
the exclusion rules (bots/mods/admins/owner/recent-joiners) and the safety cap.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot_modules.services.moderation import compute_roles_to_snapshot


@dataclass(frozen=True)
class SweepCandidate:
    """A member the sweep proposes to move to the inactive channel."""

    user_id: int
    last_seen: float  # epoch seconds; the more recent of last-message / join
    idle_seconds: float


def select_sweep_candidates(
    *,
    last_seen: dict[int, float],
    now: float,
    threshold_seconds: float,
    exclude_ids: set[int],
    cap: int,
) -> tuple[list[SweepCandidate], int]:
    """Decide which members should be swept into the inactive channel.

    ``last_seen`` maps ``user_id -> epoch seconds`` of the member's most recent
    activity signal (the caller should pass ``max(last_message_ts, joined_at)``
    so a brand-new member who hasn't posted isn't treated as ancient). Any
    member whose ID is in ``exclude_ids`` — bots, mods, admins, the owner, and
    anyone already inactive — is skipped entirely.

    A member is a candidate when ``now - last_seen >= threshold_seconds``.
    Results are sorted most-idle-first (so the cap keeps the stalest members)
    and truncated to ``cap``.

    Returns ``(candidates, overflow)`` where ``overflow`` is how many eligible
    members were dropped by the cap — the caller surfaces this so a silent
    truncation never reads as "swept everyone".
    """
    if threshold_seconds <= 0 or cap <= 0:
        return [], 0

    eligible: list[SweepCandidate] = []
    for user_id, seen in last_seen.items():
        if user_id in exclude_ids:
            continue
        idle = now - seen
        if idle >= threshold_seconds:
            eligible.append(
                SweepCandidate(user_id=user_id, last_seen=seen, idle_seconds=idle)
            )

    # Most-idle first, tie-break on user_id for a deterministic order.
    eligible.sort(key=lambda c: (-c.idle_seconds, c.user_id))

    if len(eligible) <= cap:
        return eligible, 0
    return eligible[:cap], len(eligible) - cap


@dataclass(frozen=True)
class PreviewRole:
    """One of a member's roles, flattened out of ``discord.Role``."""

    role_id: int
    name: str
    managed: bool  # integration-controlled; the bot can neither strip nor restore it
    position: int = 0


@dataclass(frozen=True)
class PreviewMember:
    """A sweep candidate's Discord state, flattened for pure processing."""

    user_id: int
    display_name: str
    roles: list[PreviewRole]


@dataclass(frozen=True)
class SweepPreviewRow:
    """What one sweep candidate would lose, for the dashboard's dry run."""

    user_id: int
    display_name: str
    idle_seconds: float
    last_seen: float
    has_tracked_messages: bool
    removed_role_ids: list[int]
    removed_role_names: list[str]
    kept_managed_role_names: list[str]


def build_sweep_preview(
    *,
    candidates: list[SweepCandidate],
    members: dict[int, PreviewMember],
    default_role_id: int,
    inactive_role_id: int,
    bot_top_role_position: int,
    tracked_user_ids: set[int],
) -> tuple[list[SweepPreviewRow], list[SweepPreviewRow]]:
    """Split sweep candidates into ``(sweepable, blocked_by_hierarchy)``.

    The role lists mirror :func:`apply_inactive`'s call site exactly: *managed*
    roles are filtered out first (an integration owns them — the bot can't strip
    them and couldn't restore them afterwards), and only then does
    :func:`compute_roles_to_snapshot` drop ``@everyone`` and ``@Inactive``. A
    member whose every role is managed is still swept, they just lose nothing but
    their channel access, so they keep a row with an empty removal list.

    ``blocked_by_hierarchy`` holds candidates with a role to strip that sits at
    or above the bot's top role: the sweep would select them and then fail on
    ``Forbidden``, counting the failure silently. They're reported apart so the
    sweepable count is one the caller can trust. Managed roles are ignored for
    this too — they are never stripped, so one outranking the bot blocks nothing.

    ``tracked_user_ids`` is who has any message history at all. A member outside
    it was aged from their join date alone — the weakest possible evidence of
    inactivity — and is marked so the dashboard can say so rather than showing a
    confident "last seen" that is really just "joined".

    Candidates with no entry in ``members`` (left the guild between selection and
    render) are dropped from both lists.
    """
    sweepable: list[SweepPreviewRow] = []
    blocked: list[SweepPreviewRow] = []

    for candidate in candidates:
        member = members.get(candidate.user_id)
        if member is None:
            continue

        strippable = [r for r in member.roles if not r.managed]
        removed_ids = compute_roles_to_snapshot(
            [r.role_id for r in strippable],
            default_role_id=default_role_id,
            jailed_role_id=inactive_role_id,
        )
        removed = set(removed_ids)

        row = SweepPreviewRow(
            user_id=candidate.user_id,
            display_name=member.display_name,
            idle_seconds=candidate.idle_seconds,
            last_seen=candidate.last_seen,
            has_tracked_messages=candidate.user_id in tracked_user_ids,
            removed_role_ids=removed_ids,
            removed_role_names=[r.name for r in strippable if r.role_id in removed],
            kept_managed_role_names=[r.name for r in member.roles if r.managed],
        )

        # Only the roles that would actually be stripped can fail on hierarchy.
        # A member's *top* role is the wrong yardstick: it may be a managed role
        # (booster, Twitch sync, another bot's role) sitting above the bot, which
        # the sweep never touches and which doesn't stop it removing the roles
        # below. Judging by top role would file such a member under "would fail"
        # and drop them from the count of who a sweep really moves.
        strip_positions = [r.position for r in strippable if r.role_id in removed]
        if strip_positions and max(strip_positions) >= bot_top_role_position:
            blocked.append(row)
        else:
            sweepable.append(row)

    return sweepable, blocked


def stale_inactive_channel_id(
    previous_raw: str | None, new_channel_id: int
) -> int | None:
    """Return the channel whose ``@Inactive`` overwrite must be revoked, if any.

    ``previous_raw`` is the stored ``inactive_channel_id`` config value (a
    string, possibly missing, empty, ``"0"``, or garbage). When ``/inactive
    panel`` re-points the inactive channel, the old channel keeps the role's
    view/send overwrite forever unless it's cleaned up — so return the old id
    when it's a real channel and genuinely different from the new one, else
    ``None``.
    """
    try:
        previous = int(previous_raw or "0")
    except ValueError:
        return None
    if previous <= 0 or previous == new_channel_id:
        return None
    return previous
