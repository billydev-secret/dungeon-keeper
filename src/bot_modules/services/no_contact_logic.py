"""Pure decision logic for the no-contact list.

Everything here takes plain Python primitives and returns plain values — no
DB access, no Discord calls. The DB layer is
``bot_modules/services/no_contact_service.py``; cogs keep the async glue.

The rule this module encodes, stated once so every gate can be checked
against it: **the bot will never put a no-contact pair in contact, and the
blocked party must never be able to tell.** Those are two requirements, not
one, and the second is the harder of the two — see :func:`blocked_outcome`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, TypeVar


class _HasId(Protocol):
    """Anything carrying a Discord snowflake — ``discord.Member`` in practice."""

    @property
    def id(self) -> int: ...


_M = TypeVar("_M", bound=_HasId)

# ── Surfaces ─────────────────────────────────────────────────────────────
# The places that record a blocked ATTEMPT, so a mod reading the log can see
# *how* someone kept trying, not just how often.
#
# Only surfaces with a discrete attempt to record appear here. Pen Pals,
# Voice Master and DM requests are gated by extending an existing predicate
# (``_is_blocked_pair``, ``effective_blocked``, ``_is_mutual``) which runs
# inside matching loops and permission syncs — there is no single moment
# there that means "he tried", and recording per loop iteration would bury
# the real attempts. They are enforced just as strictly; they simply do not
# generate log lines. Constants for them existed briefly and were removed
# rather than left as labels the dashboard could never render.

SURFACE_WHISPER = "whisper"
SURFACE_AMA = "ama"
SURFACE_AMA_ANSWER = "ama_answer"
SURFACE_CONFESSION_REPLY = "confession_reply"
SURFACE_GUESS_SUBMIT = "guess_submit"
SURFACE_GUESS = "guess"

SURFACE_LABELS = {
    SURFACE_WHISPER: "Whisper",
    SURFACE_AMA: "AMA question",
    SURFACE_AMA_ANSWER: "AMA answer DM",
    SURFACE_CONFESSION_REPLY: "Confession reply",
    SURFACE_GUESS_SUBMIT: "Guess Who submission",
    SURFACE_GUESS: "Guess Who guess",
}

# Event kinds recorded in no_contact_events.
KIND_ATTEMPT = "attempt"
KIND_MENTION = "mention"
KIND_REPLY = "reply"


def surface_label(surface: str) -> str:
    """Human label for a surface constant, falling back to the raw value."""
    return SURFACE_LABELS.get(surface, surface or "Unknown")


# ── Pair identity ────────────────────────────────────────────────────────


def pair_key(user_a: int, user_b: int) -> tuple[int, int]:
    """Normalise two user ids to the stored ``(user_low, user_high)`` ordering.

    A no-contact rule is symmetric — it does not matter who added it or who
    is trying to reach whom — so the pair is stored under a canonical
    ordering and every lookup normalises the same way. Mirrors the convention
    ``dm_consent_pairs`` uses (see ``dm_perms_service.relationship_key``).
    """
    return (user_a, user_b) if user_a < user_b else (user_b, user_a)


def is_self_pair(user_a: int, user_b: int) -> bool:
    """True when both ids are the same member — never a valid pair."""
    return user_a == user_b


# ── Blocked-contact outcome ──────────────────────────────────────────────


# ── Removal authorisation ────────────────────────────────────────────────

REMOVAL_DENIED_MUTUAL = (
    "❌ This is a moderator-set separation — neither member can lift it. "
    "Speak to a moderator."
)
REMOVAL_DENIED_MISSING = "❌ There's no no-contact entry between you two."


def can_remove(
    *,
    protected_user_id: int | None,
    actor_id: int,
    actor_is_mod: bool,
) -> bool:
    """Whether ``actor_id`` may lift a no-contact entry.

    Rules, in order:

    - a mod may always remove (staff need to clean up mistakes and handle
      cases where the protected member has left);
    - a mutual entry (``protected_user_id is None``) can only be removed by a
      mod — neither party alone;
    - otherwise only the protected member may remove it.

    The asymmetry with *adding* is deliberate. Adding is self-service and
    instant, because requiring a mod conversation puts the highest barrier
    exactly where someone is most reluctant to speak up. Removing is the
    dangerous direction: it is the step a harasser benefits from, and the one
    he might pressure or sweet-talk her into taking. Restricting it to the
    protected member also closes the case where he adds an entry himself,
    looks cooperative, and quietly removes it later.
    """
    if actor_is_mod:
        return True
    if protected_user_id is None:
        return False
    return actor_id == protected_user_id


def is_visible_to(*, protected_user_id: int | None, viewer_id: int) -> bool:
    """Whether a member may be shown that this entry exists.

    An entry that protects the OTHER member is invisible: telling him it
    exists is the disclosure the whole feature is built to prevent, and it
    would arrive through the calmest possible door — him running a list
    command and reading her name back.

    Visible to the protected member (it is theirs), and visible on a mutual
    mod-set separation, where a moderator has by definition already spoken to
    both parties. Mods see everything via the dashboard, which does not go
    through this function.
    """
    if protected_user_id is None:
        return True
    return viewer_id == protected_user_id


def removal_denied_message(
    protected_user_id: int | None, *, actor_id: int
) -> str:
    """User-facing refusal after :func:`can_remove` rejected a removal.

    Crucially, a member who may not even KNOW about the entry is told there
    is no entry — not that they lack permission to remove it. "You can't
    remove this" is a confirmation that something is there to remove, which
    is the same leak by a different route.

    Only two outcomes are reachable. A refusal on a *visible* entry can only
    mean a mutual separation: the sole other visible case is the protected
    member acting on their own entry, and :func:`can_remove` allows that, so
    it never reaches here.
    """
    if not is_visible_to(protected_user_id=protected_user_id, viewer_id=actor_id):
        return REMOVAL_DENIED_MISSING
    return REMOVAL_DENIED_MUTUAL


def resolve_protected_user(
    *, user_a: int, user_b: int, protect: int | None
) -> int | None:
    """Validate the ``protected_user_id`` for a new entry.

    ``protect`` must be one of the two members, or None for a mutual
    separation. Anything else (a third party, a typo'd id) collapses to None
    rather than storing a value that would make :func:`can_remove` grant
    removal rights to someone outside the pair.
    """
    if protect is None:
        return None
    if protect in (user_a, user_b):
        return protect
    return None


# ── Mention / reply alerting ─────────────────────────────────────────────


@dataclass(frozen=True)
class MentionAlert:
    """One alertable contact found in a posted message."""

    actor_id: int
    target_id: int
    kind: str


def alerts_for_message(
    *,
    author_id: int,
    mentioned_ids: Iterable[int],
    reply_to_author_id: int | None,
    no_contact_partners: Iterable[int],
) -> list[MentionAlert]:
    """Find no-contact contacts in one message.

    Fires on two triggers:

    - an ``@mention`` of the other member (from ``message_mentions``);
    - a reply to a message the other member authored (from
      ``messages.reply_to_id``).

    The reply trigger is not redundant. A Discord reply with the ping toggled
    off still lands in front of the person replied to, and never appears in
    ``message_mentions`` — so a mention-only alert has an obvious hole that
    takes about thirty seconds to find once someone notices alerts have
    stopped arriving.

    A message that both mentions and replies to the same person yields ONE
    alert (the mention), so a single message can't double-post to the mod
    channel. Self-mentions are ignored: a no-contact pair can't contain the
    same member twice, but a caller passing a partner set built elsewhere
    shouldn't be able to produce a self-alert.
    """
    partners = {p for p in no_contact_partners if p != author_id}
    if not partners:
        return []

    out: list[MentionAlert] = []
    seen: set[int] = set()

    for uid in mentioned_ids:
        if uid in partners and uid not in seen:
            seen.add(uid)
            out.append(MentionAlert(author_id, uid, KIND_MENTION))

    if (
        reply_to_author_id is not None
        and reply_to_author_id in partners
        and reply_to_author_id not in seen
    ):
        seen.add(reply_to_author_id)
        out.append(MentionAlert(author_id, reply_to_author_id, KIND_REPLY))

    return out


def alert_ping_prefix(alert_role_id: int) -> str:
    """Role ping to lead an alert with, or an empty string when unconfigured."""
    return f"<@&{alert_role_id}> " if alert_role_id else ""


# ── Guess Who ────────────────────────────────────────────────────────────


def guess_round_blocked_for(
    *, viewer_id: int, submitter_id: int, answer_id: int, partners: Iterable[int]
) -> bool:
    """Whether ``viewer_id`` must be kept away from a Guess Who round.

    A round has a *submitter* (who posted it) and an *answer* (who is in the
    image) as separate fields, and they are not always the same member. Both
    matter: the answer is whose likeness is on display, and the submitter is
    who chose to put it there.
    """
    partner_set = set(partners)
    return submitter_id in partner_set or answer_id in partner_set


def candidate_members_for(
    members: Iterable[_M], partners: Iterable[int]
) -> list[_M]:
    """Drop no-contact partners from a member picker.

    Removing a name from a long paginated member list is invisible to the
    person using it — there is nothing to notice missing — so this costs
    nothing on the leak side.

    Takes whatever the caller already holds (``discord.Member`` at both the
    Guess Who and AMA call sites) and returns the same objects, so the tested
    filter is the shipped filter rather than a parallel id-only version the
    cogs then re-implement by hand.
    """
    partner_set = set(partners)
    return [m for m in members if m.id not in partner_set]
