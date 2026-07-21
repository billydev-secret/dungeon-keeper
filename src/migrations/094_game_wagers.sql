-- PvP coin wagers on the duel games (economy sinks round 2, stage 4b —
-- docs/plans/economy-sinks-round-2.md; the payout funnel it depends on is
-- stage 4a, commits 1c4deab/02c0f45).
--
-- Equal ante, winner takes the pot, NO RAKE — so this moves currency
-- sideways between members and absorbs nothing. That is deliberate (the
-- round's own note): wagers exist to make the games matter, and absorption
-- targets rest on the other stages.
--
-- One row per (game_type, game_id, user_id). All wager state lives here so
-- none of the six per-game tables need a schema change:
--
--   pending  — amount declared, NO money moved (a duel challenger's ante
--              between /challenge and accept; a decline just deletes it)
--   held     — debited and escrowed, waiting on the game's terminal state
--   settled  — paid out to the winner (the winner's own row is settled too)
--   refunded — given back (abandon, void, wipeout, lobby leave, guild leave)
--
-- ``settled_at`` is the exactly-once guard for BOTH payout and refund: every
-- transition predicates on it being NULL, so a replayed terminal-state hook
-- (the sweep and the resume path can both fire one) can never pay twice.
-- The game's ante is read back off these rows (``amount`` is identical
-- across a game's rows), which is why a lobby's host is staked at creation.

CREATE TABLE IF NOT EXISTS econ_game_wagers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    game_type  TEXT    NOT NULL,           -- BaseGame.GAME_KEY
    game_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    state      TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'held', 'settled', 'refunded')),
    created_at REAL    NOT NULL,
    settled_at REAL
);

-- One stake per player per game: the race anchor for double-join / double-
-- accept clicks.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_game_wagers_player
    ON econ_game_wagers (game_type, game_id, user_id);

CREATE INDEX IF NOT EXISTS idx_econ_game_wagers_game
    ON econ_game_wagers (game_type, game_id, state);

-- Boot-time orphan sweep reads live escrow by guild.
CREATE INDEX IF NOT EXISTS idx_econ_game_wagers_live
    ON econ_game_wagers (guild_id, state);
