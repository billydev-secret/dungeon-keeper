"""Reaction tips — transfer with a burned rake, charged once per reactor.

The invariant behind every test here: a tip must never mint. Whatever leaves
the reactor either lands with the poster or is destroyed, and the two together
always equal what was debited.
"""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.auto_react_service import (
    record_placement,
    remove_auto_react_rule,
    upsert_auto_react_rule,
)
from bot_modules.services.economy_service import apply_credit, get_balance
from bot_modules.services.reaction_tip_service import (
    MIN_RAKE,
    apply_tip,
    compute_rake,
    get_rungs,
    min_rung_amount,
    plan_tip,
    replace_rungs,
    set_rung,
)

GUILD = 1
CHANNEL = 2
MESSAGE = 3
POSTER = 100
REACTOR = 200
EMOJI = "💎"
RUNG = 25


@pytest.fixture
def tippable(sync_db_path):
    """A message the bot reacted to, with a 25-coin rung configured.

    The rule itself is part of the fixture because tipping is only live while
    the channel still has a tipping rule — the charge path checks it.
    """
    upsert_auto_react_rule(
        sync_db_path, GUILD, CHANNEL, [EMOJI], True, tips_enabled=True
    )
    record_placement(
        sync_db_path,
        guild_id=GUILD,
        channel_id=CHANNEL,
        message_id=MESSAGE,
        author_id=POSTER,
        emojis=[EMOJI],
    )
    set_rung(sync_db_path, GUILD, CHANNEL, EMOJI, RUNG)
    return sync_db_path


def fund(db_path, user_id: int, amount: int) -> None:
    with open_db(db_path) as conn:
        apply_credit(conn, GUILD, user_id, amount, "test_seed")


def balance(db_path, user_id: int) -> int:
    with open_db(db_path) as conn:
        return get_balance(conn, GUILD, user_id)


def tip(db_path, **overrides):
    kwargs = dict(
        guild_id=GUILD, message_id=MESSAGE, reactor_id=REACTOR, emoji=EMOJI
    )
    kwargs.update(overrides)
    return apply_tip(db_path, **kwargs)


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paid", "expected"),
    [
        pytest.param(0, 0, id="nothing"),
        pytest.param(1, 1, id="floor-applies-below-10"),
        pytest.param(5, 1, id="five-rakes-the-floor"),
        # Half-up, not banker's: round(2.5) would give 2 and quietly shrink
        # the sink.
        pytest.param(25, 3, id="twenty-five-rounds-half-up"),
        pytest.param(100, 10, id="hundred-is-ten-percent"),
    ],
)
def test_compute_rake(paid, expected):
    # A flat percentage alone rounds to zero on small tips, which would make
    # the sink symbolic; the floor keeps every real tip net-negative.
    assert compute_rake(paid) == expected


def test_rake_never_exceeds_the_tip():
    assert compute_rake(1) == 1


@pytest.mark.parametrize(
    ("rung", "balance_", "expected"),
    [
        pytest.param(25, 500, (25, 22), id="full-tip"),
        pytest.param(5, 500, (5, 4), id="small-rung"),
        pytest.param(100, 100, (100, 90), id="exactly-affordable"),
        pytest.param(25, 10, (10, 9), id="partial-when-short"),
        pytest.param(25, 2, (2, 1), id="partial-down-to-two"),
        pytest.param(25, 1, (0, 0), id="one-coin-declined"),
        pytest.param(25, 0, (0, 0), id="broke-declined"),
    ],
)
def test_plan_tip(rung, balance_, expected):
    # A tap that would deliver the poster nothing after the burn is declined
    # outright rather than becoming a pure burn dressed up as a tip.
    assert plan_tip(rung, balance_) == expected


# --------------------------------------------------------------------------
# the transfer
# --------------------------------------------------------------------------


def test_tip_moves_coins_and_burns_the_rake(tippable):
    fund(tippable, REACTOR, 500)

    outcome = tip(tippable)

    assert outcome.charged is True
    assert (outcome.paid, outcome.delivered, outcome.burned) == (25, 22, 3)
    assert balance(tippable, REACTOR) == 475
    assert balance(tippable, POSTER) == 22


def test_tip_never_mints(tippable):
    # The whole reason this is a transfer and not a faucet.
    fund(tippable, REACTOR, 500)
    before = balance(tippable, REACTOR) + balance(tippable, POSTER)

    outcome = tip(tippable)
    after = balance(tippable, REACTOR) + balance(tippable, POSTER)

    assert after == before - outcome.burned
    assert outcome.paid == outcome.delivered + outcome.burned


def test_tip_writes_both_ledger_sides(tippable):
    fund(tippable, REACTOR, 500)

    tip(tippable)

    with open_db(tippable) as conn:
        kinds = {
            row["kind"]: row["amount"]
            for row in conn.execute(
                "SELECT kind, amount FROM econ_ledger WHERE kind IN ('tip_out','tip_in')"
            )
        }
    # economy_loop drains econ_ledger to the register channel, so these rows
    # are the only feedback a reactor gets that money moved.
    assert kinds == {"tip_out": -25, "tip_in": 22}


def test_partial_payment_when_short(tippable):
    fund(tippable, REACTOR, 10)

    outcome = tip(tippable)

    assert (outcome.paid, outcome.delivered, outcome.burned) == (10, 9, 1)
    assert balance(tippable, REACTOR) == 0
    assert balance(tippable, POSTER) == 9


def test_broke_reactor_is_a_free_no_op(tippable):
    outcome = tip(tippable)

    assert outcome.charged is False
    assert outcome.reason == "insufficient"
    assert balance(tippable, POSTER) == 0


def test_one_coin_balance_is_declined_entirely(tippable):
    # Would deliver the poster 0 after the 1-coin floor — so nothing happens
    # at all rather than the reactor paying into a void.
    fund(tippable, REACTOR, 1)

    outcome = tip(tippable)

    assert outcome.charged is False
    assert balance(tippable, REACTOR) == 1


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------


def test_second_reaction_by_the_same_user_is_free(tippable):
    fund(tippable, REACTOR, 500)
    tip(tippable)

    outcome = tip(tippable)

    assert outcome.charged is False
    assert outcome.reason == "already_charged"
    assert balance(tippable, REACTOR) == 475


def test_unreact_then_react_again_does_not_recharge(tippable):
    # There is no unreact hook by design: the award row is permanent, so a
    # removed-and-readded reaction charges once and refunds nothing.
    fund(tippable, REACTOR, 500)
    tip(tippable)
    tip(tippable)

    with open_db(tippable) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) c FROM reaction_tip_awards"
        ).fetchone()["c"]
    assert rows == 1
    assert balance(tippable, REACTOR) == 475


def test_a_different_reactor_pays_separately(tippable):
    fund(tippable, REACTOR, 500)
    fund(tippable, REACTOR + 1, 500)

    tip(tippable)
    tip(tippable, reactor_id=REACTOR + 1)

    assert balance(tippable, POSTER) == 44


def test_self_tip_is_ignored(tippable):
    fund(tippable, POSTER, 500)

    outcome = tip(tippable, reactor_id=POSTER)

    assert outcome.charged is False
    assert outcome.reason == "self"
    assert balance(tippable, POSTER) == 500


def test_bot_reactor_is_ignored(tippable):
    # The auto-react bot places these emoji itself and has no wallet.
    fund(tippable, REACTOR, 500)

    outcome = apply_tip(
        tippable,
        guild_id=GUILD,
        message_id=MESSAGE,
        reactor_id=REACTOR,
        emoji=EMOJI,
        reactor_is_bot=True,
    )

    assert outcome.charged is False
    assert outcome.reason == "bot"
    assert balance(tippable, REACTOR) == 500


def test_placed_emoji_without_a_price_is_free(tippable):
    # On the rule and on the post, but priced 0 — decoration, not a button.
    record_placement(
        tippable,
        guild_id=GUILD,
        channel_id=CHANNEL,
        message_id=MESSAGE,
        author_id=POSTER,
        emojis=[EMOJI, "😂"],
    )
    fund(tippable, REACTOR, 500)

    outcome = tip(tippable, emoji="😂")

    assert outcome.charged is False
    assert outcome.reason == "not_a_rung"
    assert balance(tippable, REACTOR) == 500


def test_emoji_the_bot_did_not_place_is_not_chargeable(sync_db_path):
    # The rung exists and the message is tippable, but this emoji never made
    # it onto the post (it failed to attach, or was added by hand). The
    # receipt lists what was actually placed, and that is what counts.
    record_placement(
        sync_db_path,
        guild_id=GUILD,
        channel_id=CHANNEL,
        message_id=MESSAGE,
        author_id=POSTER,
        emojis=["🔥"],
    )
    set_rung(sync_db_path, GUILD, CHANNEL, "🔥", 5)
    set_rung(sync_db_path, GUILD, CHANNEL, EMOJI, RUNG)
    fund(sync_db_path, REACTOR, 500)

    outcome = tip(sync_db_path)

    assert outcome.charged is False
    assert outcome.reason == "not_placed"
    assert balance(sync_db_path, REACTOR) == 500


def test_message_without_a_placement_is_not_tippable(sync_db_path):
    # A rung pasted by hand onto a text post, an old message, or an image the
    # classifier rejected must never become a payment target.
    set_rung(sync_db_path, GUILD, CHANNEL, EMOJI, RUNG)
    fund(sync_db_path, REACTOR, 500)

    outcome = tip(sync_db_path)

    assert outcome.charged is False
    assert outcome.reason == "not_tippable"
    assert balance(sync_db_path, REACTOR) == 500


def test_removing_the_rule_stops_the_emoji_being_tip_buttons(tippable):
    # The panel promises exactly this on the remove confirmation: the emoji
    # already sitting on old posts stop charging the moment the rule is gone.
    fund(tippable, REACTOR, 500)
    remove_auto_react_rule(tippable, GUILD, CHANNEL)

    outcome = tip(tippable)

    assert outcome.charged is False
    assert outcome.reason == "tips_off"
    assert balance(tippable, REACTOR) == 500
    # …and the ladder goes with the rule, so re-creating it can't inherit
    # prices nobody set.
    assert get_rungs(tippable, GUILD, CHANNEL) == {}


def test_switching_tipping_off_stops_charging_old_posts(tippable):
    # Unchecking Tips keeps the rule (and its prices, for when it comes back)
    # but nothing may be charged while the toggle is off.
    fund(tippable, REACTOR, 500)
    upsert_auto_react_rule(
        tippable, GUILD, CHANNEL, [EMOJI], True, tips_enabled=False
    )

    outcome = tip(tippable)

    assert outcome.charged is False
    assert outcome.reason == "tips_off"
    assert balance(tippable, REACTOR) == 500
    assert get_rungs(tippable, GUILD, CHANNEL) == {EMOJI: RUNG}


def test_rung_from_another_channel_does_not_apply(tippable):
    # Rungs are per-channel; the placement's channel is what counts. 🔥 is on
    # the post, so this reaches the rung lookup rather than stopping earlier.
    record_placement(
        tippable,
        guild_id=GUILD,
        channel_id=CHANNEL,
        message_id=MESSAGE,
        author_id=POSTER,
        emojis=[EMOJI, "🔥"],
    )
    fund(tippable, REACTOR, 500)
    set_rung(tippable, GUILD, CHANNEL + 1, "🔥", 100)

    outcome = tip(tippable, emoji="🔥")

    assert outcome.charged is False
    assert outcome.reason == "not_a_rung"
    assert balance(tippable, REACTOR) == 500


# --------------------------------------------------------------------------
# rung config
# --------------------------------------------------------------------------


def test_rungs_round_trip(sync_db_path):
    set_rung(sync_db_path, GUILD, CHANNEL, "🔥", 5)
    set_rung(sync_db_path, GUILD, CHANNEL, "💎", 25)

    assert get_rungs(sync_db_path, GUILD, CHANNEL) == {"🔥": 5, "💎": 25}


def test_setting_a_rung_again_replaces_the_amount(sync_db_path):
    set_rung(sync_db_path, GUILD, CHANNEL, "🔥", 5)
    set_rung(sync_db_path, GUILD, CHANNEL, "🔥", 50)

    assert get_rungs(sync_db_path, GUILD, CHANNEL) == {"🔥": 50}


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amount_clears_the_rung(sync_db_path, amount):
    set_rung(sync_db_path, GUILD, CHANNEL, "🔥", 5)
    set_rung(sync_db_path, GUILD, CHANNEL, "🔥", amount)

    assert get_rungs(sync_db_path, GUILD, CHANNEL) == {}


def test_replace_rungs_sets_exactly_the_given_ladder(sync_db_path):
    set_rung(sync_db_path, GUILD, CHANNEL, "🔥", 5)
    set_rung(sync_db_path, GUILD, CHANNEL, "💎", 25)

    # 💎 drops off the rule and 👑 joins it, in one pass.
    replace_rungs(sync_db_path, GUILD, CHANNEL, {"🔥": 5, "👑": 100})

    assert get_rungs(sync_db_path, GUILD, CHANNEL) == {"🔥": 5, "👑": 100}


def test_replace_rungs_drops_zero_priced_emoji(sync_db_path):
    replace_rungs(sync_db_path, GUILD, CHANNEL, {"🔥": 5, "😂": 0})

    assert get_rungs(sync_db_path, GUILD, CHANNEL) == {"🔥": 5}


def test_min_rung_amount_is_derived_from_the_rake():
    # Not a hardcoded 2: if the rake floor moves, so does the minimum the API
    # and the dashboard enforce.
    minimum = min_rung_amount()

    assert plan_tip(minimum, minimum)[1] >= 1
    assert plan_tip(minimum - 1, minimum - 1) == (0, 0)
    assert minimum == MIN_RAKE + 1
