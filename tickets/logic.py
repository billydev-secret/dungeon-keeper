"""Pure ticket business logic — no Discord API calls, no database access."""

from __future__ import annotations

import time
from typing import Any

# Valid ticket statuses
TICKET_OPEN = "open"
TICKET_CLOSED = "closed"
TICKET_DELETED = "deleted"

_VALID_TRANSITIONS: dict[str, set[str]] = {
    TICKET_OPEN: {TICKET_CLOSED},
    TICKET_CLOSED: {TICKET_OPEN, TICKET_DELETED},
    TICKET_DELETED: set(),
}


def next_ticket_state(current_status: str, action: str) -> str:
    """Return the next ticket status after an action.

    action: 'close', 'reopen', 'delete'

    Raises ValueError if the transition is invalid.
    """
    _action_map = {
        "close": TICKET_CLOSED,
        "reopen": TICKET_OPEN,
        "delete": TICKET_DELETED,
    }
    target = _action_map.get(action)
    if target is None:
        raise ValueError(f"Unknown action: {action!r}")
    allowed = _valid_transitions(current_status)
    if target not in allowed:
        raise ValueError(
            f"Cannot {action!r} a ticket with status {current_status!r}"
        )
    return target


def _valid_transitions(status: str) -> set[str]:
    return _VALID_TRANSITIONS.get(status, set())


def ticket_rate_limit_ok(
    last_ticket_ts: float | None,
    now_ts: float | None = None,
    window_seconds: float = 300.0,
) -> bool:
    """Return True if enough time has passed since the last ticket was opened."""
    if last_ticket_ts is None:
        return True
    if now_ts is None:
        now_ts = time.time()
    return (now_ts - last_ticket_ts) >= window_seconds


def should_escalate(ticket: dict[str, Any]) -> bool:
    """Return True if a second claimer should trigger escalation."""
    return bool(ticket.get("claimer_id")) and not ticket.get("escalated")


def can_close_ticket(ticket: dict[str, Any], closer_id: int, is_mod: bool) -> bool:
    """Return True if closer_id is allowed to close the ticket.

    Mods can always close. Non-mods can only close their own open tickets.
    """
    if is_mod:
        return True
    return ticket.get("user_id") == closer_id and ticket.get("status") == TICKET_OPEN
