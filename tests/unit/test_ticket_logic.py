"""Tier 1 unit tests: ticket pure logic."""

import time

import pytest
from freezegun import freeze_time

from tickets.logic import (
    TICKET_CLOSED,
    TICKET_DELETED,
    TICKET_OPEN,
    can_close_ticket,
    next_ticket_state,
    should_escalate,
    ticket_rate_limit_ok,
)


# ── next_ticket_state ─────────────────────────────────────────────────

def test_close_open_ticket():
    assert next_ticket_state(TICKET_OPEN, "close") == TICKET_CLOSED


def test_reopen_closed_ticket():
    assert next_ticket_state(TICKET_CLOSED, "reopen") == TICKET_OPEN


def test_delete_closed_ticket():
    assert next_ticket_state(TICKET_CLOSED, "delete") == TICKET_DELETED


def test_cannot_close_deleted():
    with pytest.raises(ValueError):
        next_ticket_state(TICKET_DELETED, "close")


def test_cannot_delete_open():
    with pytest.raises(ValueError):
        next_ticket_state(TICKET_OPEN, "delete")


def test_unknown_action_raises():
    with pytest.raises(ValueError):
        next_ticket_state(TICKET_OPEN, "explode")


# ── ticket_rate_limit_ok ──────────────────────────────────────────────

@freeze_time("2026-04-23 12:00:00")
def test_no_previous_ticket_always_ok():
    assert ticket_rate_limit_ok(None) is True


@freeze_time("2026-04-23 12:00:00")
def test_recent_ticket_blocked():
    now = time.time()
    assert ticket_rate_limit_ok(now - 60, now_ts=now, window_seconds=300) is False


@freeze_time("2026-04-23 12:00:00")
def test_old_ticket_allowed():
    now = time.time()
    assert ticket_rate_limit_ok(now - 600, now_ts=now, window_seconds=300) is True


@freeze_time("2026-04-23 12:00:00")
def test_exactly_at_window_allowed():
    now = time.time()
    assert ticket_rate_limit_ok(now - 300, now_ts=now, window_seconds=300) is True


# ── should_escalate ───────────────────────────────────────────────────

def test_escalate_when_already_claimed():
    ticket = {"claimer_id": 1001, "escalated": 0}
    assert should_escalate(ticket) is True


def test_no_escalate_when_not_claimed():
    ticket = {"claimer_id": None, "escalated": 0}
    assert should_escalate(ticket) is False


def test_no_escalate_already_escalated():
    ticket = {"claimer_id": 1001, "escalated": 1}
    assert should_escalate(ticket) is False


# ── can_close_ticket ──────────────────────────────────────────────────

def test_mod_can_always_close():
    ticket = {"user_id": 3001, "status": TICKET_OPEN}
    assert can_close_ticket(ticket, closer_id=2001, is_mod=True) is True


def test_owner_can_close_own_open_ticket():
    ticket = {"user_id": 3001, "status": TICKET_OPEN}
    assert can_close_ticket(ticket, closer_id=3001, is_mod=False) is True


def test_non_owner_cannot_close():
    ticket = {"user_id": 3001, "status": TICKET_OPEN}
    assert can_close_ticket(ticket, closer_id=9999, is_mod=False) is False
