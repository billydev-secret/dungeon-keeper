"""Pure decision logic for the Spin-the-Compliment cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from its
button callbacks; the Discord glue (sending the message, persisting via
``modify_payload``) stays in the cog.

Reusable pieces:

* :func:`join_participant` / :func:`leave_participant` — the Join and
  Leave button spines (two buttons since 2026-09-04, social-prompt-46;
  the old single button was an unlabelled toggle that a double-tap
  silently undid).
* :func:`generate_pairings` — a thin wrapper around the shared
  derangement helper, carrying the no-contact pairs for the pool so a
  blocked pair is never giver→receiver in either direction. It's exposed
  here so tests don't need to import the shared util directly, and so
  the cog has one place to swap the algorithm if the rules ever change.
* :func:`delivered_givers` — the wrap-up's one question, answered from
  the message archive: which givers have replied to, or @mentioned, their
  receiver in the channel since the pairings went out (social-prompt-39).
* :func:`wrap_schedule` / :func:`wrap_seconds_remaining` — the timing
  of the ten-minute wrap: a nudge halfway, the recap at the end, and how
  much of it is left after a restart.

``serialize_pairings`` / :func:`parse_pairings` and :func:`pairing_ids`
are tiny dict transformations the cog used to inline; pulling them out
makes the end-game payload handoff testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bot_modules.games.utils.derangement import random_derangement

# The wrap-up: after the pairings post, the game stays open this long so
# the compliments actually get given before the recap pays the room.
WRAP_SECONDS = 600
# When the pairings are reposted with a ✅ per giver who has delivered, and
# the stragglers get their one nudge.
WRAP_NUDGE_AFTER_SECONDS = 300

# games_active_games.state while the wrap runs — a lobby is "joining", so
# the idle-lobby sweep and recovery can tell the two apart.
STATE_WRAPPING = "wrapping"


def join_participant(payload: dict[str, Any], user_id: int) -> bool:
    """Add ``user_id`` to the pool. Returns False when already in it."""
    participants: list[int] = payload.setdefault("participants", [])
    if user_id in participants:
        return False
    participants.append(user_id)
    return True


def leave_participant(payload: dict[str, Any], user_id: int) -> bool:
    """Remove ``user_id`` from the pool. Returns False when not in it."""
    participants: list[int] = payload.setdefault("participants", [])
    if user_id not in participants:
        return False
    participants.remove(user_id)
    return True


def generate_pairings(
    participants: list[int],
    forbidden_pairs: Iterable[tuple[int, int]] | None = None,
) -> dict[int, int]:
    """Return ``{giver_id: receiver_id}`` for the close-and-generate step.

    Thin wrapper around the shared derangement helper. Kept here so the
    cog imports from its sibling module rather than reaching into
    ``games.utils`` directly, and so future variants (weighted matching)
    can swap in without touching the cog.

    ``forbidden_pairs`` is the no-contact set for the pool, as
    ``no_contact_pairs_among`` returns it (``(low, high)`` tuples); a
    blocked pair is never paired in either direction.

    Returns an empty dict when fewer than 2 participants are supplied, and
    also when the forbidden pairs leave no valid pairing — the cog treats
    both the same way (its ordinary "need at least 2 players" refusal),
    so the protected member can't tell which one fired.
    """
    return random_derangement(participants, forbidden_pairs)


def serialize_pairings(pairings: dict[int, int]) -> dict[str, int]:
    """Stringify pairing keys for the persisted payload.

    Discord's payload-JSON convention uses string keys for user IDs.
    The cog writes this into ``end_game(payload=...)`` so the round
    output survives a bot restart.
    """
    return {str(giver): receiver for giver, receiver in pairings.items()}


def pairing_ids(pairings: dict[int, int]) -> list[int]:
    """Return every user id that appears in ``pairings``.

    Used to build the public mentions list when the cog announces the
    pairings — every giver AND every receiver is pinged so each player
    sees their assignment land in their notification tray. Order is
    preserved (givers in iteration order, then their receiver) and
    duplicates are de-duped while preserving order.
    """
    seen: dict[int, None] = {}
    for giver, receiver in pairings.items():
        if giver not in seen:
            seen[giver] = None
        if receiver not in seen:
            seen[receiver] = None
    return list(seen.keys())


def parse_pairings(raw: Any) -> dict[int, int]:
    """Inverse of :func:`serialize_pairings`: the stored ``{"giver": receiver}``
    map back to ints. Tolerates a missing or malformed value (empty map)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[int, int] = {}
    for giver, receiver in raw.items():
        try:
            out[int(giver)] = int(receiver)
        except (TypeError, ValueError):
            continue
    return out


def delivered_givers(
    conn,
    *,
    guild_id: int,
    channel_id: int,
    since_ts: int,
    pairings: dict[int, int],
) -> set[int]:
    """Givers whose compliment has been seen: a message of theirs in the
    game's channel since ``since_ts`` that replies to their receiver or
    @mentions them.

    Reads the message archive's metadata only (``messages.reply_to_id``,
    ``message_mentions``), which is recorded whatever the guild's content
    storage level is — so this works with message content off, the default.
    A giver who compliments in a bare message with no reply and no mention
    is not counted; the wrap's copy tells them how to be.
    """
    if not pairings:
        return set()
    givers = list(pairings)
    marks = ",".join("?" for _ in givers)
    rows = conn.execute(
        f"""
        SELECT m.author_id, r.author_id AS replied_to, mm.user_id AS mentioned
        FROM messages m
        LEFT JOIN messages r ON r.message_id = m.reply_to_id
        LEFT JOIN message_mentions mm ON mm.message_id = m.message_id
        WHERE m.guild_id = ? AND m.channel_id = ? AND m.ts >= ?
          AND m.author_id IN ({marks})
        """,
        (int(guild_id), int(channel_id), int(since_ts), *[int(g) for g in givers]),
    ).fetchall()
    delivered: set[int] = set()
    for author_id, replied_to, mentioned in rows:
        receiver = pairings.get(int(author_id))
        if receiver is None:
            continue
        if (replied_to is not None and int(replied_to) == receiver) or (
            mentioned is not None and int(mentioned) == receiver
        ):
            delivered.add(int(author_id))
    return delivered


def stragglers(pairings: dict[int, int], delivered: Iterable[int]) -> list[int]:
    """Givers still owing a compliment, in pairing order."""
    done = set(delivered)
    return [giver for giver in pairings if giver not in done]


def wrap_schedule(generated_at: int) -> tuple[int, int]:
    """``(nudge_at, ends_at)`` epochs for a wrap that started at ``generated_at``."""
    return generated_at + WRAP_NUDGE_AFTER_SECONDS, generated_at + WRAP_SECONDS


def wrap_seconds_remaining(target_epoch: Any, now_epoch: int, *, floor: int = 5) -> int:
    """Seconds until ``target_epoch`` for re-arming a wrap after a restart —
    never below ``floor`` so an overdue beat fires promptly rather than
    never. A missing target reads as "now"."""
    try:
        target = int(target_epoch)
    except (TypeError, ValueError):
        return floor
    return max(floor, target - int(now_epoch))


def delivered_line(delivered_count: int, total: int) -> str:
    """The recap's one number."""
    if total == 0:
        return "No pairings this round."
    if delivered_count == total:
        return f"💛 All **{total}** compliments delivered!"
    return f"💛 **{delivered_count}** of **{total}** compliments delivered."
