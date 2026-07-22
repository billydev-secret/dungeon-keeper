-- Frozen quest-board pool per period.
--
-- The personal board is a pure function of (pool, user, period_idx, n).
-- Reading the LIVE active set on every view meant an admin activating or
-- deactivating a quest — or changing a board size — mid-period reshuffled
-- every member's CURRENT board, because both m = len(pool) and n move the
-- draw window (start = index * n % m). Adding one quest shifted everyone's
-- other slots, not just the added one.
--
-- Fix: snapshot the active pool and board size the first time any member's
-- board of a cadence is computed in a period, and draw the rest of that
-- period from the snapshot. Library and board-size edits then only surface
-- at the next daily/weekly/monthly roll — the pool a member sees is frozen
-- for the period they're in.
--
-- pool_json: {"<quest_id>": "<pair_tag>", …} for the active cadence pool at
-- freeze time (pair_tag carried so apply_pair_bundles stays stable too).
-- board_size: n frozen from the guild's settings at freeze time.
-- One row per (guild, qtype, period_idx); rows for older periods are pruned
-- on insert, since a board is only ever read for the current period.
CREATE TABLE IF NOT EXISTS econ_quest_pool_snapshots (
    guild_id   INTEGER NOT NULL,
    qtype      TEXT    NOT NULL,
    period_idx INTEGER NOT NULL,
    pool_json  TEXT    NOT NULL,
    board_size INTEGER NOT NULL,
    PRIMARY KEY (guild_id, qtype, period_idx)
) WITHOUT ROWID;
