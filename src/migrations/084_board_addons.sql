-- Board add-ons (plan: quest-variety stage 5): reroll, set bonus.
--
-- econ_board_overrides — the personal board is a pure function of
-- (pool, user, period_index, n); a reroll swaps one slot for the period, so
-- it needs a persisted override applied on top of the pure draw. Keyed by
-- period_index (the integer the draw itself uses). from_quest_id is the
-- PURE slot being replaced — a same-period re-reroll of the replacement
-- updates to_quest_id in place, so application never chains.
--
-- econ_rerolls — one free reroll per member per guild-local day.
--
-- econ_set_bonus — reservation rows for the clear-your-board bonus
-- (one per member per cadence period), the exactly-once guard.

CREATE TABLE IF NOT EXISTS econ_board_overrides (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    qtype         TEXT    NOT NULL,
    period_idx    INTEGER NOT NULL,
    from_quest_id INTEGER NOT NULL,
    to_quest_id   INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, qtype, period_idx, from_quest_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS econ_rerolls (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    local_day TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, local_day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS econ_set_bonus (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    qtype    TEXT    NOT NULL,
    period   TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, qtype, period)
) WITHOUT ROWID;
