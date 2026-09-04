"""Pure decision logic for the Anonymous AMA cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and modal handlers; the Discord glue (sending the
message, persisting via ``modify_payload``) stays in the cog.

High-leverage pieces:

* :func:`utcnow_iso` / :func:`parse_iso_ts` — ISO-8601 timestamp
  serialization and round-tripping used throughout payload entries.
* :func:`is_resolved_status` / :data:`RESOLVED_QUESTION_STATUSES` —
  central definition of "this question is done, drop its view".
* :func:`build_question_entry` — the shape every newly-asked question
  dict has when first appended to the payload's ``questions`` list.
* :func:`mark_question_*` — small status-transition helpers that
  preserve the cog's ``setdefault`` semantics for idempotency
  (``asked_at`` / ``hot_seat_id`` / ``question_message_id``).
* :func:`recompute_totals` — refreshes ``total_answered`` from the
  current status of each question (the cog's existing predicate).
* :func:`should_expire` — pure retention check used to prune stale
  unanswered question views after 7 days.
* :func:`open_question_cards` / :func:`pending_screened_dms` — the two
  sets of live buttons a close or a restart has to find again: posted
  question cards still waiting on the hot seat, and screened questions
  still sitting in the host's DMs.
* :func:`hot_seat_seconds_remaining` — how much of the hot seat's hour is
  left after a restart, so the auto-rotate timer is re-armed rather than
  dropped.
* :func:`compute_recap_stats` — pulls the ``_do_close`` totals
  (unique askers, rotations, etc.) out of the close path so the recap
  embed builder can take a plain dict.
* :func:`bottom_bar_label` — collapses the bottom-bar label formatting
  duplicated between ``_update_bottom_bar`` and ``_resend_ama_bottom``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# Game formats. "hot_seat" is the classic one-person-at-a-time rotation;
# "panel" lists everyone who opted in so any of them can be asked directly.
AMA_FORMAT_HOT_SEAT = "hot_seat"
AMA_FORMAT_PANEL = "panel"

# Retention horizon for unanswered questions before their view is
# pruned and the entry flipped to "expired".
UNANSWERED_QUESTION_RETENTION = timedelta(days=7)

# How long one person holds the hot seat before the seat rotates on its own.
HOT_SEAT_SECONDS = 3600

# Answered (or passed) questions per hot-seat turn before the seat rotates.
# The dashboard's "Questions per Turn" dial overrides it per server; the
# clamp keeps a stored 0 or 999 from producing a seat that never moves or
# never settles.
DEFAULT_QUESTIONS_PER_TURN = 4
MIN_QUESTIONS_PER_TURN = 1
MAX_QUESTIONS_PER_TURN = 20

# Title Case labels for the two moderation modes (embed_style_guide). The
# payload keeps the lowercase enum; only the embeds go through this map.
MODE_LABELS = {
    "unfiltered": "Unfiltered",
    "screened": "Screened",
}

# Status values the cog treats as "terminal" — once a question is in
# one of these states, its persistent view is removed and it stops
# counting toward future actions.
RESOLVED_QUESTION_STATUSES: set[str] = {
    "answered",
    "passed",
    "rejected",
    "expired",
}


def utcnow_iso(now: datetime | None = None) -> str:
    """Return the current UTC time as an ISO-8601 string.

    ``now`` is injected for tests so the timestamp can be pinned; in
    production callers omit it and the real ``datetime.now`` is used.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return now.isoformat()


def parse_iso_ts(value: Any) -> datetime | None:
    """Parse a stored ISO-8601 string back into a UTC ``datetime``.

    Tolerates ``None``/empty input (returns ``None``) and unrecognized
    strings (also ``None``). Naive datetimes are coerced to UTC so the
    return value is always timezone-aware.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_resolved_status(status: str | None) -> bool:
    """Return ``True`` if ``status`` is one of the terminal states."""
    return (status or "").lower() in RESOLVED_QUESTION_STATUSES


def should_expire(
    asked_at: datetime | None,
    now: datetime,
    retention: timedelta = UNANSWERED_QUESTION_RETENTION,
) -> bool:
    """Return whether an unanswered question is older than ``retention``.

    Returns ``False`` if ``asked_at`` is ``None`` — a missing
    timestamp shouldn't trigger expiry, mirroring the cog's
    ``if asked_at and (now - asked_at) > UNANSWERED_QUESTION_RETENTION``
    guard.
    """
    if asked_at is None:
        return False
    return (now - asked_at) > retention


def build_question_entry(
    asker_id: int,
    text: str,
    hot_seat_id: int,
    *,
    status: str = "pending",
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Return the dict appended to ``payload["questions"]`` for a new ask.

    ``status`` is ``"pending"`` for player questions (flipped to
    ``"approved"`` once the message has been posted).
    """
    return {
        "asker_id": asker_id,
        "text": text,
        "status": status,
        "asked_at": now_iso if now_iso is not None else utcnow_iso(),
        "hot_seat_id": hot_seat_id,
    }


def add_question(payload: dict[str, Any], entry: dict[str, Any]) -> int:
    """Append ``entry`` to ``payload['questions']`` and refresh the count.

    Returns the new question's 0-based index, matching the
    ``q_idx = len(...) - 1`` pattern the cog uses after persisting.
    """
    questions: list[dict[str, Any]] = payload.setdefault("questions", [])
    questions.append(entry)
    payload["total_questions"] = len(questions)
    return len(questions) - 1


def mark_question_approved(
    payload: dict[str, Any],
    q_idx: int,
    *,
    message_id: int | None = None,
    hot_seat_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    """Flip a question's status to ``"approved"`` and stamp the post.

    Mirrors both code paths:

    * Unfiltered mode (always-approve) sets ``status`` and
      ``question_message_id`` unconditionally; ``asked_at`` and
      ``hot_seat_id`` are already present from
      :func:`build_question_entry`, so neither is forwarded.
    * Screened-mode host approval also sets ``status`` and
      ``question_message_id`` unconditionally, but uses ``setdefault``
      for ``asked_at`` / ``hot_seat_id`` to preserve the asker's
      original timestamp on re-approval.
    """
    questions: list[dict[str, Any]] = payload.get("questions", [])
    if q_idx >= len(questions):
        return
    q = questions[q_idx]
    q["status"] = "approved"
    if message_id is not None:
        q["question_message_id"] = message_id
    if hot_seat_id is not None:
        q.setdefault("hot_seat_id", hot_seat_id)
    if now_iso is not None:
        q.setdefault("asked_at", now_iso)



def mark_question_answered(
    payload: dict[str, Any],
    q_idx: int,
    *,
    message_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    """Flip a question's status to ``"answered"`` and stamp the time.

    Also refreshes ``payload["total_answered"]`` so the recap and
    progress bar pick up the new answered count in the same write.
    """
    questions: list[dict[str, Any]] = payload.get("questions", [])
    if q_idx < len(questions):
        q = questions[q_idx]
        q["status"] = "answered"
        q["answered_at"] = now_iso if now_iso is not None else utcnow_iso()
        if message_id is not None:
            q.setdefault("question_message_id", message_id)
    recompute_totals(payload)


def mark_question_passed(
    payload: dict[str, Any],
    q_idx: int,
    *,
    message_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    """Flip a question's status to ``"passed"`` and bump the pass total."""
    questions: list[dict[str, Any]] = payload.get("questions", [])
    if q_idx < len(questions):
        q = questions[q_idx]
        q["status"] = "passed"
        q["passed_at"] = now_iso if now_iso is not None else utcnow_iso()
        if message_id is not None:
            q.setdefault("question_message_id", message_id)
    payload["total_passed"] = payload.get("total_passed", 0) + 1


def mark_question_pending_dm(
    payload: dict[str, Any], q_idx: int, dm_message_id: int,
) -> None:
    """Record the host-DM message a screened question is waiting on.

    The approval buttons live on that DM. Its id is what lets a restart
    re-register them (:func:`pending_screened_dms`) instead of leaving the
    question stuck in the queue with dead buttons.
    """
    questions: list[dict[str, Any]] = payload.get("questions", [])
    if q_idx < len(questions):
        questions[q_idx]["host_dm_message_id"] = dm_message_id


def mark_question_rejected(payload: dict[str, Any], q_idx: int) -> None:
    """Flip a screened question's status to ``"rejected"``.

    No timestamp or message id is stored — rejected questions never
    leave the host's DMs.
    """
    questions: list[dict[str, Any]] = payload.get("questions", [])
    if q_idx < len(questions):
        questions[q_idx]["status"] = "rejected"


def mark_question_expired(
    payload_question: dict[str, Any],
    *,
    now_iso: str | None = None,
) -> None:
    """Flip a single question dict to ``"expired"`` in place.

    Operates on the question entry directly (not the payload) because
    the prune loops iterate ``questions`` in the payload and decide
    which entries to expire individually.
    """
    payload_question["status"] = "expired"
    payload_question["expired_at"] = now_iso if now_iso is not None else utcnow_iso()


def recompute_totals(payload: dict[str, Any]) -> None:
    """Refresh ``total_answered`` from the current question statuses.

    The cog re-derives this denormalised count in several places using
    the same comprehension; centralising it here keeps the predicate
    consistent.
    """
    questions: list[dict[str, Any]] = payload.get("questions", [])
    payload["total_answered"] = sum(
        1 for q in questions if q.get("status") == "answered"
    )


def unique_asker_count(questions: Iterable[dict[str, Any]]) -> int:
    """Return the number of distinct askers with a real member id.

    Every question is asked by a member now; the ``> 0`` filter is a
    cheap defence against a malformed entry (old payloads once carried a
    bot-seeded ``asker_id`` of 0) so it can never count as a person.
    """
    return len({q["asker_id"] for q in questions if q.get("asker_id", 0) > 0})


def question_message_id(question: dict[str, Any]) -> int | None:
    """The posted card's message id, under either key the payload has used."""
    mid = question.get("question_message_id") or question.get("message_id")
    return int(mid) if mid else None


def open_question_cards(questions: Iterable[dict[str, Any]]) -> list[tuple[int, int]]:
    """``(index, message_id)`` for every posted question still awaiting the
    hot seat — the cards whose Reply / Pass buttons must be retired when the
    game ends, or re-registered after a restart."""
    out: list[tuple[int, int]] = []
    for idx, q in enumerate(questions):
        mid = question_message_id(q)
        if mid is None or is_resolved_status(q.get("status")):
            continue
        out.append((idx, mid))
    return out


def pending_screened_dms(questions: Iterable[dict[str, Any]]) -> list[tuple[int, int]]:
    """``(index, host_dm_message_id)`` for every screened question the host
    has not yet approved or rejected and whose approval DM is known."""
    out: list[tuple[int, int]] = []
    for idx, q in enumerate(questions):
        if (q.get("status") or "").lower() != "pending":
            continue
        dm_id = q.get("host_dm_message_id")
        if dm_id:
            out.append((idx, int(dm_id)))
    return out


def hot_seat_seconds_remaining(
    started_at: Any,
    now: datetime,
    *,
    total: int = HOT_SEAT_SECONDS,
    floor: int = 5,
) -> int:
    """Seconds left of the hot seat's hour, for re-arming the timer after a
    restart. An unknown start (old payload) gets the full hour; a seat whose
    hour already elapsed while the bot was down gets ``floor`` seconds so it
    rotates promptly rather than never (the vote games' own rule)."""
    started = parse_iso_ts(started_at)
    if started is None:
        return total
    remaining = total - int((now - started).total_seconds())
    return max(floor, min(total, remaining))


def resolve_questions_per_turn(value: Any) -> int:
    """Clamp the dashboard's "Questions per Turn" dial into a usable turn."""
    try:
        n = int(value) if value not in (None, "") else DEFAULT_QUESTIONS_PER_TURN
    except (TypeError, ValueError):
        n = DEFAULT_QUESTIONS_PER_TURN
    return max(MIN_QUESTIONS_PER_TURN, min(MAX_QUESTIONS_PER_TURN, n))


def mode_label(mode: Any) -> str:
    """Title Case label for a stored mode ("screened" → "Screened")."""
    key = str(mode or "").lower()
    return MODE_LABELS.get(key, key.title() or "Unfiltered")


def compute_recap_stats(payload: dict[str, Any]) -> dict[str, int]:
    """Summarise a finished AMA payload for the recap embed.

    Returns the same fields the existing ``_do_close`` derived inline:

    * ``total_q``        — number of questions appended
    * ``total_answered`` — denormalised answered count
    * ``total_passed``   — denormalised passed count
    * ``rotations``      — number of hot-seat changes during the game
    * ``unique_askers``  — distinct askers (a malformed id 0 never counts)
    """
    questions: list[dict[str, Any]] = payload.get("questions", [])
    return {
        "total_q": len(questions),
        "total_answered": payload.get("total_answered", 0),
        "total_passed": payload.get("total_passed", 0),
        "rotations": payload.get("hot_seat_rotations", 0),
        "unique_askers": unique_asker_count(questions),
    }


def normalize_format(value: Any) -> str:
    """Coerce a stored/param format string to a known value.

    Anything that isn't the explicit panel sentinel falls back to the
    classic hot-seat format, so old payloads (which have no ``format``
    key) and bad input both behave as hot-seat.
    """
    return AMA_FORMAT_PANEL if value == AMA_FORMAT_PANEL else AMA_FORMAT_HOT_SEAT


def toggle_panel_member(panel: list[int], uid: int) -> bool:
    """Add or remove ``uid`` from ``panel`` in place (panel format).

    Returns ``True`` when the user just joined the panel and ``False``
    when a second tap removed them. Order is preserved so the roster
    renders in join order.
    """
    if uid in panel:
        panel.remove(uid)
        return False
    panel.append(uid)
    return True


def is_panel_target(panel: list[int], target_id: int) -> bool:
    """Return whether ``target_id`` is still a valid panelist to ask.

    Used to reject a question whose chosen target left the panel between
    the dropdown opening and the modal being submitted.
    """
    return target_id in panel


def panel_bottom_bar_label(panel_size: int) -> str:
    """Format the persistent bottom-bar content for a panel-format game."""
    if panel_size:
        suffix = "s" if panel_size != 1 else ""
        return f"🎙️ AMA Panel — {panel_size} answering question{suffix}"
    return "🎙️ AMA Panel"


def bottom_bar_label(hot_seat_name: str | None, queue_len: int) -> str:
    """Format the persistent bottom-bar content string.

    Used by both ``_update_bottom_bar`` (live edit) and
    ``_resend_ama_bottom`` (re-post when chat scrolls). Returning a
    plain string keeps the two call sites in lock-step.
    """
    queue_str = f"  •  📋 {queue_len} in queue" if queue_len else ""
    if hot_seat_name:
        return f"🎙️ AMA: @{hot_seat_name}{queue_str}"
    return "🎙️ AMA"


def remaining_questions_text(
    questions_this_turn: int, per_turn: int = DEFAULT_QUESTIONS_PER_TURN,
) -> str:
    """Return the ``"N question(s) left this turn."`` blurb.

    ``questions_this_turn`` counts questions the seat has *answered or
    passed* (social-prompt-34): a question still waiting on the seat does
    not use up the turn, so a slow answerer is never rotated out with
    cards still open. Never goes below zero.
    """
    remaining = max(0, per_turn - questions_this_turn)
    suffix = "s" if remaining != 1 else ""
    return f"**{remaining}** question{suffix} left this turn."
