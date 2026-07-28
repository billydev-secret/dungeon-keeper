"""Currency payouts for intake-card checklist steps.

The greeter who ticks a step on a newcomer's intake card earns a flat award
(``EconSettings.reward_intake_step``), plus any ``intake_step`` trigger quest
stacked on top — the same two-payout shape as the Photo Challenge faucet.

Paid **per step** rather than per finished card, deliberately: a card is
completed by whoever posts the completion code, and
``intake_service.complete_card`` stamps every unticked step as *skipped* and
records that one person as the welcomer of record. Paying on completion would
hand the whole award to the code-poster even when someone else did the work,
and would pay nothing at all for a shared or half-finished intake.

Two properties this module has to guarantee, both enforced here rather than
by the caller:

* **A step pays once, ever.** The manual step button in ``intake_views`` is a
  *toggle* — unticking clears ``done_at``/``done_by`` — so the step's own
  state cannot be the dedup key or a greeter could mint coins by clicking one
  step on and off. The ``econ_intake_rewards`` anchor (migration 138) is
  keyed on ``(guild_id, card_id, step_key)`` with no user id, so neither the
  original ticker nor a different greeter can claim it twice.
* **Only real people are paid.** ``intake_service.auto_tick`` records
  ``AUTO_ACTOR`` (0) for steps that tick from a role change, so ``verified``
  and ``role_gained`` steps credit nobody. Only the ``greeted`` auto-tick
  (which carries the greeting author's id) and the manual buttons pay.

Every payout rides the caller's transaction — the credit commits with the
tick or not at all — but each step is wrapped in its own SAVEPOINT so an
economy failure can never roll back the intake tick itself. Economy must
never block intake flow.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_quests_service import (
    fire_trigger_quests,
    source_enabled,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    load_econ_settings,
)

log = logging.getLogger(__name__)

#: Income-source key / quest trigger kind for this faucet.
SOURCE = "intake_step"


def pay_intake_steps(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    card_id: int,
    newcomer_id: int,
    step_keys: Sequence[str],
    actor_id: int,
    booster: bool,
    at: float,
) -> int:
    """Pay the greeter for the steps they just ticked; returns coins credited.

    ``step_keys`` are the keys that actually changed to done in this call —
    the caller already knows them (``auto_tick`` returns them, the button
    knows its own). Steps already anchored pay nothing, so re-ticking a
    toggled step is silently free.

    Returns 0 without touching the ledger when the economy is off, the source
    is disabled, the tick has no human actor, or the greeter is the newcomer.
    """
    if not step_keys:
        return 0
    # No human to pay: role-change auto-ticks record AUTO_ACTOR (0).
    if actor_id <= 0:
        return 0
    # A member ticking a step on their own card can't pay themselves.
    if actor_id == newcomer_id:
        return 0

    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return 0
    if not source_enabled(conn, guild_id, SOURCE):
        return 0

    offset = get_tz_offset_hours(conn, guild_id)
    day = local_day_for(at, offset)
    total = 0
    for step_key in step_keys:
        total += _pay_one_step(
            conn,
            settings,
            guild_id,
            card_id=card_id,
            step_key=step_key,
            actor_id=actor_id,
            booster=booster,
            day=day,
            at=at,
        )
    return total


def _pay_one_step(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    *,
    card_id: int,
    step_key: str,
    actor_id: int,
    booster: bool,
    day: str,
    at: float,
) -> int:
    """One step's flat award + stacked quest, inside its own SAVEPOINT."""
    savepoint = "intake_reward"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        credited = 0
        # Flat award — the anchor is what makes it once-per-step-per-card.
        # A 0 reward skips the anchor entirely so the quest below can still
        # fire (and so raising the reward later isn't blocked by rows written
        # while it was off), mirroring the photo_post faucet.
        if settings.reward_intake_step > 0:
            cur = conn.execute(
                "INSERT OR IGNORE INTO econ_intake_rewards"
                " (guild_id, card_id, step_key, user_id, amount, awarded_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    guild_id,
                    card_id,
                    step_key,
                    actor_id,
                    settings.reward_intake_step,
                    at,
                ),
            )
            if (cur.rowcount or 0) == 1:
                credited = apply_credit(
                    conn,
                    guild_id,
                    actor_id,
                    settings.reward_intake_step,
                    SOURCE,
                    meta={"card_id": card_id, "step": step_key},
                    booster=booster,
                    multiplier=settings.booster_multiplier,
                )
                # Record what was actually banked (booster multiplier applied)
                # so the table reconciles against the ledger.
                conn.execute(
                    "UPDATE econ_intake_rewards SET amount = ?"
                    " WHERE guild_id = ? AND card_id = ? AND step_key = ?",
                    (credited, guild_id, card_id, step_key),
                )
        # The intake_step quest stacks on top, keyed per card+step so a
        # counted quest ("tick 10 intake steps this week") advances once per
        # step. fire_trigger_quests re-checks the source toggle itself.
        fire_trigger_quests(
            conn,
            settings,
            guild_id,
            SOURCE,
            actor_id,
            local_day=day,
            occurrence=f"{card_id}:{step_key}",
            booster=booster,
        )
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        log.exception(
            "econ intake: payout failed for card %s step %s in guild %s",
            card_id,
            step_key,
            guild_id,
        )
        return 0
    finally:
        conn.execute(f"RELEASE {savepoint}")
    return credited
