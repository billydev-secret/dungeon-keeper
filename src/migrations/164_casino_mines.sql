-- Migration 164: Mines (docs/plans/casino-mines.md) — the tenth casino
-- table and the third live-hand game, after blackjack and war.
--
-- A 20-tile grid (5 wide × 4 tall) hides a player-chosen number of bombs
-- (1/3/5/10). Each safe reveal steps a multiplier; cashing out banks it; a
-- bomb takes the stake. The row exists for the whole of the round rather
-- than only a decision point (war's shape), because every press between
-- the deal and the stop is a decision the player could walk away from.
--
-- Same live-hand contract as the other two, and it is the contract the
-- money safety rests on:
--   * one live grid per member per guild — the partial unique index, not
--     just the caller's pre-check, so a raced second deal raises
--     IntegrityError and rolls back with the stake;
--   * exactly-once settlement via settled_at IS NULL, so a player's cash
--     out, the idle auto-cash and a boot sweep can all reach the same row
--     and only the first one pays;
--   * last_action_at drives the idle auto-cash (blackjack_idle_seconds,
--     shared by all three games), bumped on every reveal.
--
-- state_json holds {"bombs": [tile, ...], "revealed": [tile, ...]}. Bomb
-- positions are drawn ONCE at deal and never re-rolled — pre-committing is
-- the version that cannot quietly grow adaptive difficulty later. Reveal
-- count is derived from that list rather than stored beside it, so the two
-- can never disagree; `bombs` is a column because the ladder lookup and the
-- stats need it without parsing JSON.

CREATE TABLE IF NOT EXISTS casino_mines_hands (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    message_id     INTEGER NOT NULL DEFAULT 0,  -- backfilled after send
    user_id        INTEGER NOT NULL,
    stake          INTEGER NOT NULL,
    bombs          INTEGER NOT NULL,            -- 1 | 3 | 5 | 10
    state_json     TEXT    NOT NULL,            -- {bombs: [...], revealed: [...]}
    outcome        TEXT,                        -- cashed|pushed|bombed|refunded
    created_at     REAL    NOT NULL,
    last_action_at REAL    NOT NULL,
    settled_at     REAL                         -- exactly-once guard
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_mines_live
    ON casino_mines_hands (guild_id, user_id) WHERE settled_at IS NULL;
