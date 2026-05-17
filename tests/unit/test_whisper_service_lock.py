"""Tier 1: age-lock + terminal-for-sender helpers (pure)."""
from __future__ import annotations

from bot_modules.services.whisper_models import Whisper
from bot_modules.services.whisper_service import (
    LOCK_DURATION_SECONDS,
    is_locked,
    is_terminal_for_sender,
)

TARGET = 2001
SENDER = 1001
NOW = 1_700_000_000.0


def _w(
    *,
    created_at: float = NOW,
    solved: bool = False,
    exposed: bool = False,
    guesses_left: int = 3,
) -> Whisper:
    return Whisper(
        id=1, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="x",
        created_at=created_at, state="pending", solved=solved, exposed=exposed,
        guesses_left=guesses_left, channel_msg_id=None, dm_msg_id=None,
    )


# ── is_locked ────────────────────────────────────────────────────────────────

def test_fresh_whisper_not_locked():
    assert is_locked(_w(), now=NOW) is False


def test_one_second_past_lock_is_locked():
    w = _w(created_at=NOW - LOCK_DURATION_SECONDS - 1)
    assert is_locked(w, now=NOW) is True


def test_exact_lock_boundary_not_locked():
    """Strict `>` so age == LOCK_DURATION_SECONDS is still active."""
    w = _w(created_at=NOW - LOCK_DURATION_SECONDS)
    assert is_locked(w, now=NOW) is False


# ── is_terminal_for_sender ──────────────────────────────────────────────────

def test_pending_in_flight_is_not_terminal():
    assert is_terminal_for_sender(_w(), now=NOW) is False


def test_solved_but_not_exposed_is_not_terminal():
    """Target may still hit Expose — sender keeps it visible."""
    assert is_terminal_for_sender(_w(solved=True), now=NOW) is False


def test_exposed_is_terminal():
    assert is_terminal_for_sender(_w(exposed=True), now=NOW) is True


def test_out_of_guesses_without_solve_is_terminal():
    """Target burned all guesses, never identified sender — sender stays anon forever."""
    assert is_terminal_for_sender(_w(guesses_left=0, solved=False), now=NOW) is True


def test_age_locked_is_terminal():
    w = _w(created_at=NOW - LOCK_DURATION_SECONDS - 1)
    assert is_terminal_for_sender(w, now=NOW) is True


def test_solved_with_remaining_guesses_not_terminal():
    """Edge: target solved on first try, two guesses unused — sender keeps it visible
    until exposure (or age lock)."""
    assert is_terminal_for_sender(_w(solved=True, guesses_left=2), now=NOW) is False
