"""Tests for economy/intake_rewards — the intake_step faucet.

The tested unit is :func:`pay_intake_steps`: who gets credited for ticking a
step on a newcomer's intake card, and every guard that stops a payout. The
Discord buttons in intake_views are glue exercised through this layer.

The load-bearing case is :func:`test_retick_after_untick_pays_nothing` — the
manual step button is a toggle, so without the econ_intake_rewards anchor a
greeter could mint coins by clicking one step off and on.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.intake_rewards import SOURCE, pay_intake_steps
from bot_modules.services import intake_service as svc
from bot_modules.services.economy_quests_service import set_income_source
from bot_modules.services.economy_service import save_econ_settings
from tests.db_template import migrated_db

GUILD = 42
NEWCOMER = 7
GREETER = 501
OTHER_GREETER = 502
NOW = 1_700_000_000.0
STEP = "sfw_questions"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "intake_rewards.db"
    migrated_db(path)
    return path


def _setup(conn, *, reward=5, enabled=True):
    """Economy on with a known intake-step rate, and a card to pay against."""
    save_econ_settings(
        conn,
        GUILD,
        {"enabled": enabled, "reward_intake_step": reward, "booster_multiplier": 2.0},
    )
    card_id = svc.create_card(conn, GUILD, NEWCOMER, NOW)
    assert card_id is not None
    return card_id


def _balance(conn, user_id=GREETER):
    row = conn.execute(
        "SELECT balance FROM econ_wallets WHERE guild_id = ? AND user_id = ?",
        (GUILD, user_id),
    ).fetchone()
    return int(row["balance"]) if row else 0


def _pay(conn, card_id, *, steps=(STEP,), actor=GREETER, booster=False):
    return pay_intake_steps(
        conn,
        GUILD,
        card_id=card_id,
        newcomer_id=NEWCOMER,
        step_keys=steps,
        actor_id=actor,
        booster=booster,
        at=NOW,
    )


# ── happy path ────────────────────────────────────────────────────────


def test_ticking_a_step_pays_the_greeter(db_path):
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id) == 5
        assert _balance(conn) == 5
        row = conn.execute(
            "SELECT * FROM econ_ledger WHERE guild_id = ? AND user_id = ?",
            (GUILD, GREETER),
        ).fetchone()
        assert row["kind"] == SOURCE


def test_several_steps_each_pay(db_path):
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id, steps=("greeted", "sfw_questions")) == 10
        assert _balance(conn) == 10


def test_booster_multiplier_applies_and_is_recorded(db_path):
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id, booster=True) == 10
        assert _balance(conn) == 10
        # The anchor stores what was actually banked, not the base rate, so it
        # reconciles against the ledger.
        amount = conn.execute(
            "SELECT amount FROM econ_intake_rewards WHERE guild_id = ? AND card_id = ?",
            (GUILD, card_id),
        ).fetchone()["amount"]
        assert amount == 10


# ── the anchor: once per (card, step), forever ────────────────────────


def test_retick_after_untick_pays_nothing(db_path):
    """The toggle hole: untick clears done_by, but the anchor survives."""
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id) == 5
        # Greeter unticks the step (clears done_at/done_by) and re-ticks it.
        svc.set_step_state(
            conn, card_id, STEP, done=False, actor_id=GREETER, at=NOW
        )
        assert _pay(conn, card_id) == 0
        assert _balance(conn) == 5


def test_a_different_greeter_cannot_reclaim_the_same_step(db_path):
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id) == 5
        assert _pay(conn, card_id, actor=OTHER_GREETER) == 0
        assert _balance(conn, OTHER_GREETER) == 0


def test_same_step_on_a_different_card_pays_again(db_path):
    """The anchor is per card — a second newcomer is genuinely new work."""
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id) == 5
        other_card = svc.create_card(conn, GUILD, NEWCOMER + 1, NOW)
        assert other_card is not None
        assert pay_intake_steps(
            conn,
            GUILD,
            card_id=other_card,
            newcomer_id=NEWCOMER + 1,
            step_keys=(STEP,),
            actor_id=GREETER,
            booster=False,
            at=NOW,
        ) == 5
        assert _balance(conn) == 10


# ── guards ────────────────────────────────────────────────────────────


def test_auto_actor_ticks_pay_nobody(db_path):
    """verified / role_gained auto-ticks record AUTO_ACTOR (0)."""
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id, actor=svc.AUTO_ACTOR) == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM econ_intake_rewards"
        ).fetchone()["n"] == 0


def test_newcomer_cannot_pay_themselves(db_path):
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id, actor=NEWCOMER) == 0
        assert _balance(conn, NEWCOMER) == 0


def test_economy_off_pays_nothing(db_path):
    with open_db(db_path) as conn:
        card_id = _setup(conn, enabled=False)
        assert _pay(conn, card_id) == 0
        assert _balance(conn) == 0


def test_source_toggle_off_pays_nothing(db_path):
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        set_income_source(conn, GUILD, SOURCE, False)
        assert _pay(conn, card_id) == 0
        assert _balance(conn) == 0


def test_source_defaults_on_with_no_dashboard_row(db_path):
    """Absent econ_income_sources row = enabled; this faucet ships ON."""
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM econ_income_sources WHERE guild_id = ?",
            (GUILD,),
        ).fetchone()["n"] == 0
        assert _pay(conn, card_id) == 5


def test_zero_rate_pays_nothing_and_leaves_no_anchor(db_path):
    """A 0 rate must not anchor, so raising the rate later still pays."""
    with open_db(db_path) as conn:
        card_id = _setup(conn, reward=0)
        assert _pay(conn, card_id) == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM econ_intake_rewards"
        ).fetchone()["n"] == 0
        save_econ_settings(conn, GUILD, {"reward_intake_step": 5})
        assert _pay(conn, card_id) == 5


def test_no_steps_ticked_pays_nothing(db_path):
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        assert _pay(conn, card_id, steps=()) == 0


def test_step_code_paste_pays_once_per_step(db_path):
    """The step-code path: pasting a canned message ticks and pays.

    Drives the same sequence ``intake_views.handle_intake_message`` does —
    evaluate_message → set_step_state → pay_intake_steps — because one message
    may carry a greet *and* a step code, and a re-paste must earn nothing.
    """
    import json

    from bot_modules.core.db_utils import set_config_value

    with open_db(db_path) as conn:
        card_id = _setup(conn)
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, "555", GUILD)
        set_config_value(
            conn,
            svc.STEPS_KEY,
            json.dumps(
                [
                    {"key": "greeted", "label": "Greeted", "auto": "greeted"},
                    {"key": STEP, "label": "SFW questions", "code": "sfw done"},
                ]
            ),
            GUILD,
        )
        # The card was created before the step config above, so rebuild it
        # against the coded steps the way a real join would.
        svc.delete_card(conn, card_id)
        card_id = svc.create_card(conn, GUILD, NEWCOMER, NOW)
        assert card_id is not None

        def _paste():
            paid = 0
            actions = svc.evaluate_message(
                conn,
                GUILD,
                channel_id=555,
                content="All SFW done — welcome!",
                mentioned_ids=[NEWCOMER],
                author_is_greeter=True,
                author_is_mod=False,
            )
            for action, uid, step_key in actions:
                if action != svc.ACTION_STEP:
                    continue
                if svc.set_step_state(
                    conn, card_id, step_key, done=True, actor_id=GREETER, at=NOW
                ):
                    paid += _pay(conn, card_id, steps=(step_key,))
            return paid

        assert _paste() == 5
        assert _balance(conn) == 5
        # Re-pasting on a still-ticked step is stopped by set_step_state.
        assert _paste() == 0
        # The case that needs the anchor: untick the step (clearing done_at,
        # so set_step_state ticks again) and re-paste. Only the anchor is
        # standing between this and an unbounded faucet.
        svc.set_step_state(
            conn, card_id, STEP, done=False, actor_id=GREETER, at=NOW
        )
        assert _paste() == 0
        assert _balance(conn) == 5


def test_skipped_steps_never_reach_the_faucet(db_path):
    """complete_card stamps unticked steps skipped with done_by NULL.

    Nothing calls the faucet for them — this pins that the completion path
    pays nobody for work that was shortcut rather than done.
    """
    with open_db(db_path) as conn:
        card_id = _setup(conn)
        completed = svc.complete_card(conn, GUILD, NEWCOMER, GREETER, NOW)
        assert completed is not None
        _card, skipped = completed
        assert skipped  # the whole checklist was shortcut
        rows = conn.execute(
            "SELECT done_by, skipped FROM intake_card_steps WHERE card_id = ?",
            (card_id,),
        ).fetchall()
        assert all(r["skipped"] and r["done_by"] is None for r in rows)
        assert _balance(conn) == 0
