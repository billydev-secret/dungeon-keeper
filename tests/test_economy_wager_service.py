"""Tests for services/economy_wager_service.py — PvP coin-wager escrow.

The money-critical paths: a failed debit RAISES (it must block the game
starting, inverting the "economy never blocks game flow" rule), every terminal
path either settles or refunds, both are exactly-once under the replayed
terminal hook, and the pot is conserved — no coin is minted or lost across a
full game.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import (
    apply_credit,
    get_balance,
    save_econ_settings,
)
from bot_modules.services.economy_wager_service import (
    declare_stake,
    drop_pending,
    game_ante,
    hold_stake,
    live_stakes_for_member,
    orphaned_games,
    pot_total,
    refund_game,
    refund_player,
    settle,
    staked_players,
)
from migrations import apply_migrations_sync

GUILD = 700
A, B, C = 2001, 2002, 2003
GAME = "chicken"
GID = 55
NOW = 1_800_000_000.0


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _fund(conn, user_id, amount):
    apply_credit(conn, GUILD, user_id, amount, "grant")


def _kinds(conn, user_id):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger WHERE guild_id = ? "
            "AND user_id = ? ORDER BY id",
            (GUILD, user_id),
        )
    ]


# ── staking ────────────────────────────────────────────────────────────


def test_hold_debits_and_escrows(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hold_stake(conn, GUILD, GAME, GID, A, 50)
        assert get_balance(conn, GUILD, A) == 50
        assert pot_total(conn, GAME, GID) == 50
        assert staked_players(conn, GAME, GID) == [A]
        assert game_ante(conn, GAME, GID) == 50
        assert _kinds(conn, A)[-1] == ("wager_stake", -50)


def test_hold_raises_when_short_and_moves_nothing(db):
    with open_db(db) as conn:
        _fund(conn, A, 10)
        with pytest.raises(ValueError, match="you have 10"):
            hold_stake(conn, GUILD, GAME, GID, A, 50)
        assert get_balance(conn, GUILD, A) == 10
        assert pot_total(conn, GAME, GID) == 0


def test_double_click_does_not_double_charge(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hold_stake(conn, GUILD, GAME, GID, A, 50)
        hold_stake(conn, GUILD, GAME, GID, A, 50)  # replayed click
        assert get_balance(conn, GUILD, A) == 50
        assert pot_total(conn, GAME, GID) == 50


def test_declare_then_hold_promotes_without_double_charge(db):
    """A duel challenger declares at challenge time, pays at accept."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        declare_stake(conn, GUILD, GAME, GID, A, 40)
        assert game_ante(conn, GAME, GID) == 40  # amount known before payment
        assert pot_total(conn, GAME, GID) == 0  # …but nothing moved yet
        assert get_balance(conn, GUILD, A) == 100

        hold_stake(conn, GUILD, GAME, GID, A, 40)
        assert get_balance(conn, GUILD, A) == 60
        assert pot_total(conn, GAME, GID) == 40


def test_declined_challenge_drops_pending_with_no_money_moved(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        declare_stake(conn, GUILD, GAME, GID, A, 40)
        drop_pending(conn, GAME, GID)
        assert game_ante(conn, GAME, GID) == 0
        assert get_balance(conn, GUILD, A) == 100
        assert _kinds(conn, A) == [("grant", 100)]  # no wager rows at all


def test_hold_rejects_non_positive(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        with pytest.raises(ValueError, match="at least 1"):
            hold_stake(conn, GUILD, GAME, GID, A, 0)


# ── settlement ─────────────────────────────────────────────────────────


def test_settle_pays_whole_pot_to_winner_and_conserves(db):
    with open_db(db) as conn:
        for uid in (A, B, C):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)
        assert pot_total(conn, GAME, GID) == 150

        paid, rake = settle(conn, GAME, GID, A)
        assert paid == 150 and rake == 0
        assert get_balance(conn, GUILD, A) == 200  # 50 left + 150 pot
        assert get_balance(conn, GUILD, B) == 50
        assert get_balance(conn, GUILD, C) == 50
        # Conserved: 300 in, 300 out — a wager mints nothing.
        total = sum(get_balance(conn, GUILD, u) for u in (A, B, C))
        assert total == 300
        assert _kinds(conn, A)[-1] == ("wager_payout", 150)


def test_settle_is_exactly_once_under_replayed_hook(db):
    with open_db(db) as conn:
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)

        assert settle(conn, GAME, GID, A).paid == 100
        # The sweep, the resume path and the resolution can all reach the hook.
        assert settle(conn, GAME, GID, A) == (0, 0)
        assert settle(conn, GAME, GID, B) == (0, 0)
        assert get_balance(conn, GUILD, A) == 150
        assert get_balance(conn, GUILD, B) == 50


def test_settle_with_no_winner_refunds_everyone(db):
    """Chicken wipeout / Musical Chairs degenerate round: winner is None."""
    with open_db(db) as conn:
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)

        assert settle(conn, GAME, GID, None) == (0, 0)
        assert get_balance(conn, GUILD, A) == 100
        assert get_balance(conn, GUILD, B) == 100
        assert pot_total(conn, GAME, GID) == 0


def test_settle_on_unfunded_game_is_a_noop(db):
    with open_db(db) as conn:
        assert settle(conn, GAME, GID, A) == (0, 0)
        assert get_balance(conn, GUILD, A) == 0


# ── refunds ────────────────────────────────────────────────────────────


def test_refund_game_returns_every_stake_once(db):
    """ABANDONED / VOID / cancelled lobby."""
    with open_db(db) as conn:
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)

        out = refund_game(conn, GAME, GID)
        assert out == {A: 50, B: 50}
        assert get_balance(conn, GUILD, A) == 100
        assert refund_game(conn, GAME, GID) == {}  # replay safe
        assert get_balance(conn, GUILD, A) == 100


def test_refund_player_covers_lobby_leave(db):
    with open_db(db) as conn:
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)

        assert refund_player(conn, GAME, GID, B) == 50
        assert get_balance(conn, GUILD, B) == 100
        assert staked_players(conn, GAME, GID) == [A]
        assert refund_player(conn, GAME, GID, B) == 0  # replay safe
        # The remaining player still settles normally.
        assert settle(conn, GAME, GID, A).paid == 50


def test_refund_after_settle_pays_nothing(db):
    """The ordering guard: a late refund can't claw back a paid pot."""
    with open_db(db) as conn:
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)
        settle(conn, GAME, GID, A)

        assert refund_game(conn, GAME, GID) == {}
        assert get_balance(conn, GUILD, A) == 150
        assert get_balance(conn, GUILD, B) == 50


def test_member_leaving_guild_finds_their_live_stakes(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hold_stake(conn, GUILD, GAME, GID, A, 50)
        hold_stake(conn, GUILD, "quickdraw", 99, A, 20)

        rows = live_stakes_for_member(conn, GUILD, A)
        assert {(r["game_type"], int(r["game_id"])) for r in rows} == {
            (GAME, GID), ("quickdraw", 99),
        }
        for row in rows:
            refund_player(conn, str(row["game_type"]), int(row["game_id"]), A)
        assert get_balance(conn, GUILD, A) == 100
        assert live_stakes_for_member(conn, GUILD, A) == []


def test_orphan_sweep_narrows_to_old_held_escrow(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hold_stake(conn, GUILD, GAME, GID, A, 50)
        _fund(conn, B, 100)
        hold_stake(conn, GUILD, GAME, 56, B, 50)
        # Stamp both explicitly — hold_stake uses wall-clock time, which would
        # otherwise sit on whichever side of the fixed NOW the test runs.
        conn.execute(
            "UPDATE econ_game_wagers SET created_at = ? WHERE user_id = ?",
            (NOW - 7200, A),  # stale
        )
        conn.execute(
            "UPDATE econ_game_wagers SET created_at = ? WHERE user_id = ?",
            (NOW - 60, B),  # fresh — not swept
        )

        assert orphaned_games(conn, older_than=NOW - 3600) == [(GAME, GID)]


# ── house rake (wager_rake_pct) ────────────────────────────────────────


def _set_rake(conn, pct):
    save_econ_settings(conn, GUILD, {"wager_rake_pct": pct})


def test_rake_comes_out_of_a_contested_pot(db):
    with open_db(db) as conn:
        _set_rake(conn, 10)
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)

        assert settle(conn, GAME, GID, A) == (90, 10)
        assert get_balance(conn, GUILD, A) == 140  # 50 left + 90 net pot
        # The 10 evaporated: 200 funded, 190 remain across both wallets.
        assert get_balance(conn, GUILD, A) + get_balance(conn, GUILD, B) == 190
        # The payout ledger row is net and names the cut in meta.
        row = conn.execute(
            "SELECT amount, meta FROM econ_ledger WHERE kind = 'wager_payout'",
        ).fetchone()
        import json

        assert int(row["amount"]) == 90
        assert json.loads(row["meta"])["rake"] == 10


def test_rake_zero_default_preserves_winner_takes_all(db):
    with open_db(db) as conn:  # no settings row at all — the shipped default
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)
        assert settle(conn, GAME, GID, A) == (100, 0)


def test_rake_never_touches_refunds(db):
    with open_db(db) as conn:
        _set_rake(conn, 10)
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)
        assert settle(conn, GAME, GID, None) == (0, 0)
        assert get_balance(conn, GUILD, A) == 100
        assert get_balance(conn, GUILD, B) == 100


def test_rake_skips_a_single_stake_pot(db):
    """A winner reclaiming their own ante (everyone else refunded out)."""
    with open_db(db) as conn:
        _set_rake(conn, 10)
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 50)
        refund_player(conn, GAME, GID, B)  # lobby leave
        assert settle(conn, GAME, GID, A) == (50, 0)
        assert get_balance(conn, GUILD, A) == 100


def test_rake_floor_division_rounds_tiny_cuts_to_nothing(db):
    with open_db(db) as conn:
        _set_rake(conn, 10)
        for uid in (A, B):
            _fund(conn, uid, 100)
            hold_stake(conn, GUILD, GAME, GID, uid, 4)  # pot 8 → 10% = 0.8 → 0
        assert settle(conn, GAME, GID, A) == (8, 0)
