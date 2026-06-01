"""Pure decision logic for the Would-You-Rather cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and modal handlers; the Discord glue (sending the
message, persisting via :func:`update_game_payload`) stays in the cog.

WYR doesn't share the full traditional/T-or-D spine (no opt-in
categories, no weighted question target — questions come from an
external bank or a FIFO user-submitted queue). What it does have is:

- :func:`parse_question_input` — split ``"a | b"`` into a tuple, or
  return ``None`` when the input is malformed.
- :func:`toggle_vote` — flip a user's vote between the two option lists
  in place. WYR's analog of the traditional cog's ``toggle_pref`` — it
  mutates two mutable lists rather than a payload dict, but the shape
  ("returns metadata the caller echoes back to the user") is identical.
- :func:`next_button_label` — render the ``"⏭️ Next (N queued)"`` label
  consistently from the queue length. Used in two places in the cog
  (modal submit and round carry-over) so it's worth centralizing.
"""

from __future__ import annotations


def parse_question_input(text: str) -> tuple[str, str] | None:
    """Parse the optional ``question`` slash-command argument.

    The format is ``"option a | option b"`` with a literal ``|`` between
    the two options. Both halves must be non-empty after stripping. An
    empty or whitespace-only ``text`` returns ``None`` (the cog then
    falls back to the question bank).

    Returns the ``(option_a, option_b)`` tuple, or ``None`` when the
    input is missing OR malformed — the cog distinguishes the two by
    inspecting the original ``text.strip()`` value before calling.
    """
    if not text.strip():
        return None
    parts = text.split("|", 1)
    if len(parts) != 2:
        return None
    a = parts[0].strip()
    b = parts[1].strip()
    if not a or not b:
        return None
    return (a, b)


def toggle_vote(
    votes_a: list[int],
    votes_b: list[int],
    user_id: int,
    choice: str,
) -> bool:
    """Record ``user_id``'s vote for ``choice`` (``"a"`` or ``"b"``).

    Mutates ``votes_a`` / ``votes_b`` in place: removes the user from
    the other side first (so toggling counts as a switch, not a double-
    vote) then appends to the chosen side if they're not already there.

    Returns ``True`` when the user was on the OTHER side just before
    this call (so the cog can append " (changed)" to its ephemeral
    confirmation), ``False`` for a fresh vote or a no-op re-press.
    """
    if choice not in ("a", "b"):
        raise ValueError(f"choice must be 'a' or 'b', got {choice!r}")

    if choice == "a":
        same_side, other_side = votes_a, votes_b
    else:
        same_side, other_side = votes_b, votes_a

    changed = user_id in other_side
    if changed:
        other_side.remove(user_id)
    if user_id not in same_side:
        same_side.append(user_id)
    return changed


def next_button_label(queued_count: int) -> str:
    """Render the WYR ``Next`` button label given the queued-question count.

    Centralized so the modal's on-submit handler and the round carry-
    over branch both produce identical text.
    """
    return f"⏭️ Next ({queued_count} queued)"
