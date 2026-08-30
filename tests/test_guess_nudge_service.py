"""Tests for the Guess inactivity nudge (``services/guess_nudge_service``).

``guess_inactivity_ping_hours`` sat in the Config Advisor for months with no
reader — the dial promised "hours of silence before a nudge" and nothing ever
nudged. These cover the decision layer that now backs it: which round counts as
stalled, what silences the nudge, and the once-per-round guard that stops a
long-unsolved round pinging the role on every loop tick.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.guess_repo import (
    insert_guess,
    insert_round,
    mark_round_solved,
    set_guess_config_value,
    set_round_answer_optout,
    soft_delete_round,
)
from bot_modules.services.guess_nudge_service import (
    MAX_QUIET_HOURS,
    NUDGED_ROUND_KEY,
    build_nudge_content,
    find_stalled_round,
    record_nudge,
)

GUILD = 9001
CHANNEL = 5551
ROLE = 7771
USER_A = 1001
USER_B = 1002
HOUR = 3600.0


def _round(conn, *, age_hours: float = 0.0, **kw) -> int:
    rid = insert_round(
        conn, guild_id=kw.pop("guild_id", GUILD), submitter_id=USER_A,
        answer_id=USER_B, channel_id=CHANNEL, message_id=42, **kw,
    )
    if age_hours:
        conn.execute(
            "UPDATE guess_rounds SET created_at = ? WHERE id = ?",
            (time.time() - age_hours * HOUR, rid),
        )
    return rid


def _enable(conn, hours: int = 4) -> None:
    set_guess_config_value(conn, GUILD, "guess_channel_id", str(CHANNEL))
    set_guess_config_value(conn, GUILD, "guess_role_id", str(ROLE))
    set_guess_config_value(conn, GUILD, "guess_inactivity_ping_hours", str(hours))


def test_round_quiet_past_the_threshold_is_stalled(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=5)
        stalled = find_stalled_round(conn, GUILD)
    assert stalled is not None
    assert stalled.round_id == rid
    assert stalled.channel_id == CHANNEL
    assert stalled.message_id == 42


def test_round_younger_than_the_threshold_is_not_stalled(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        _round(conn, age_hours=3)
        assert find_stalled_round(conn, GUILD) is None


def test_a_recent_guess_resets_the_silence(sync_db_path: Path):
    """Activity is the newest guess, not the round's age."""
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=30)
        insert_guess(
            conn, round_id=rid, guesser_id=USER_B,
            guessed_user_id=USER_A, correct=False,
        )
        assert find_stalled_round(conn, GUILD) is None


def test_an_old_guess_still_leaves_the_round_stalled(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=30)
        insert_guess(
            conn, round_id=rid, guesser_id=USER_B,
            guessed_user_id=USER_A, correct=False,
        )
        conn.execute(
            "UPDATE guess_guesses SET created_at = ? WHERE round_id = ?",
            (time.time() - 9 * HOUR, rid),
        )
        stalled = find_stalled_round(conn, GUILD)
    assert stalled is not None and stalled.round_id == rid


@pytest.mark.parametrize("hours", [0, -1])
def test_zero_or_negative_hours_disables_the_nudge(sync_db_path: Path, hours: int):
    with open_db(sync_db_path) as conn:
        _enable(conn, hours=hours)
        _round(conn, age_hours=48)
        assert find_stalled_round(conn, GUILD) is None


def test_no_configured_channel_disables_the_nudge(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        set_guess_config_value(conn, GUILD, "guess_role_id", str(ROLE))
        set_guess_config_value(conn, GUILD, "guess_inactivity_ping_hours", "4")
        _round(conn, age_hours=48)
        assert find_stalled_round(conn, GUILD) is None


def test_no_configured_role_disables_the_nudge(sync_db_path: Path):
    """Nothing to ping means the nudge would be a bare bump — skip it."""
    with open_db(sync_db_path) as conn:
        set_guess_config_value(conn, GUILD, "guess_channel_id", str(CHANNEL))
        set_guess_config_value(conn, GUILD, "guess_inactivity_ping_hours", "4")
        _round(conn, age_hours=48)
        assert find_stalled_round(conn, GUILD) is None


def test_solved_rounds_are_never_stalled(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=48)
        mark_round_solved(
            conn, round_id=rid, solver_id=USER_B,
            guesses_to_solve=1, unique_guessers_to_solve=1,
        )
        assert find_stalled_round(conn, GUILD) is None


def test_deleted_rounds_are_never_stalled(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=48)
        soft_delete_round(conn, rid)
        assert find_stalled_round(conn, GUILD) is None


def test_opted_out_rounds_are_never_stalled(sync_db_path: Path):
    """A member who left the Guess role can't be guessed — don't rally a hunt."""
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=48)
        set_round_answer_optout(conn, rid)
        assert find_stalled_round(conn, GUILD) is None


def test_another_guilds_round_is_invisible(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        _round(conn, age_hours=48, guild_id=GUILD + 1)
        assert find_stalled_round(conn, GUILD) is None


def test_a_round_is_only_nudged_once(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=48)
        assert find_stalled_round(conn, GUILD).round_id == rid
        record_nudge(conn, GUILD, rid)
        assert find_stalled_round(conn, GUILD) is None


def test_a_newer_stalled_round_nudges_after_an_earlier_one(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        first = _round(conn, age_hours=48)
        record_nudge(conn, GUILD, first)
        second = _round(conn, age_hours=6)
        stalled = find_stalled_round(conn, GUILD)
    assert stalled is not None and stalled.round_id == second


def test_the_oldest_stalled_round_is_picked_first(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        older = _round(conn, age_hours=48)
        _round(conn, age_hours=6)
        assert find_stalled_round(conn, GUILD).round_id == older


def test_a_round_quiet_for_months_is_never_nudged(sync_db_path: Path):
    """The bug: a round from May pinged the role "quiet for 2704 hours"."""
    with open_db(sync_db_path) as conn:
        _enable(conn)
        _round(conn, age_hours=2704)
        assert find_stalled_round(conn, GUILD) is None


def test_a_round_quiet_for_thirty_hours_is_still_nudged(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=30)
        stalled = find_stalled_round(conn, GUILD)
    assert stalled is not None and stalled.round_id == rid


@pytest.mark.parametrize(
    ("age_hours", "nudged"),
    [
        pytest.param(MAX_QUIET_HOURS - 1, True, id="just-inside"),
        pytest.param(MAX_QUIET_HOURS + 1, False, id="just-outside"),
    ],
)
def test_the_ceiling_bounds_how_stale_a_nudge_may_get(
    sync_db_path: Path, age_hours: float, nudged: bool
):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=age_hours)
        stalled = find_stalled_round(conn, GUILD)
    assert (stalled is not None and stalled.round_id == rid) is nudged


def test_an_ancient_backlog_does_not_hide_a_recent_stalled_round(sync_db_path: Path):
    """Before the ceiling the oldest round won forever, and the queue crawled.

    Production had 31 unsolved rounds from May ahead of everything recent, and
    the once-per-round guard remembers a single id — so each tick burned one
    ancient round instead of reaching the ones worth bumping.
    """
    with open_db(sync_db_path) as conn:
        _enable(conn)
        for _ in range(3):
            _round(conn, age_hours=2704)
        recent = _round(conn, age_hours=30)
        stalled = find_stalled_round(conn, GUILD)
    assert stalled is not None and stalled.round_id == recent


def test_the_oldest_round_inside_the_window_is_still_picked_first(sync_db_path: Path):
    """Oldest-first survives, bounded: the round closest to ageing out goes now."""
    with open_db(sync_db_path) as conn:
        _enable(conn)
        older = _round(conn, age_hours=MAX_QUIET_HOURS - 2)
        _round(conn, age_hours=30)
        assert find_stalled_round(conn, GUILD).round_id == older


def test_a_dial_set_beyond_the_ceiling_nudges_nothing(sync_db_path: Path):
    """The panel now stops at the ceiling, but a value stored under the old
    720-hour bound can still be sitting in a guild's config — past the ceiling
    the window is empty and the nudge stays quiet rather than misfiring."""
    with open_db(sync_db_path) as conn:
        _enable(conn, hours=int(MAX_QUIET_HOURS) + 24)
        _round(conn, age_hours=MAX_QUIET_HOURS + 12)
        assert find_stalled_round(conn, GUILD) is None


def test_record_nudge_stores_the_round_id(sync_db_path: Path):
    from bot_modules.core.db_utils import get_config_value
    with open_db(sync_db_path) as conn:
        record_nudge(conn, GUILD, 77)
        assert get_config_value(conn, NUDGED_ROUND_KEY, "0", GUILD) == "77"


def test_nudge_content_pings_the_role_and_links_the_round(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        rid = _round(conn, age_hours=5)
        stalled = find_stalled_round(conn, GUILD)
    content = build_nudge_content(stalled, guild_id=GUILD)
    assert f"<@&{ROLE}>" in content
    assert f"/{GUILD}/{CHANNEL}/42" in content
    assert str(rid)  # the round exists; the link is what members click


def test_nudge_content_omits_the_link_without_a_message(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _enable(conn)
        _round(conn, age_hours=5)
        conn.execute("UPDATE guess_rounds SET message_id = 0")
        stalled = find_stalled_round(conn, GUILD)
    content = build_nudge_content(stalled, guild_id=GUILD)
    assert f"<@&{ROLE}>" in content
    assert "discord.com/channels" not in content
