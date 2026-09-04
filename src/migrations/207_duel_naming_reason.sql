-- Migration 207: why a duel ended with no nickname, and the two one-shot pings
-- (2026-09-04, docs/reviews/2026-09-02-games-deep-review-findings.md
-- duels-party-115 / duels-party-118).
--
-- nick_reason: NO_NICK_SET used to lump four different endings together —
-- the winner never pressed the button, the loser outranks the bot, the loser
-- left the server, the loser was already serving a sentence — so the state
-- lied about what happened. Every path that concludes a game without a rename
-- now writes one of: winner_timeout, loser_outranks, loser_left, winner_left,
-- already_serving, superseded (see duels/db.py NICK_REASONS). Rows written
-- before this stay NULL: nothing can recover which of the four it was.
--
-- nick_reminded_at: the winner gets one in-channel ping partway through the
-- naming window. Persisted rather than held in memory so a restart cannot
-- ping twice.
--
-- lobby_warned_at: the lobby host gets one ping shortly before an idle lobby
-- closes. A later join resets the lobby clock, and the sweep re-arms the
-- warning by comparing this against last_action_at, so it lives on the row.
-- Group tables only — duels have no lobby.
--
-- Per-user data: none new. All six tables are already registered under the
-- party-games row in docs/data_register.md and purged with it.
--
-- Idempotent: the migration runner tolerates "duplicate column name".

ALTER TABLE pressure_games   ADD COLUMN nick_reason TEXT;
ALTER TABLE quickdraw_games  ADD COLUMN nick_reason TEXT;
ALTER TABLE hot_potato_games ADD COLUMN nick_reason TEXT;
ALTER TABLE hp_group_games   ADD COLUMN nick_reason TEXT;
ALTER TABLE chicken_games    ADD COLUMN nick_reason TEXT;
ALTER TABLE mc_games         ADD COLUMN nick_reason TEXT;

ALTER TABLE pressure_games   ADD COLUMN nick_reminded_at REAL;
ALTER TABLE quickdraw_games  ADD COLUMN nick_reminded_at REAL;
ALTER TABLE hot_potato_games ADD COLUMN nick_reminded_at REAL;
ALTER TABLE hp_group_games   ADD COLUMN nick_reminded_at REAL;
ALTER TABLE chicken_games    ADD COLUMN nick_reminded_at REAL;
ALTER TABLE mc_games         ADD COLUMN nick_reminded_at REAL;

ALTER TABLE hp_group_games   ADD COLUMN lobby_warned_at REAL;
ALTER TABLE chicken_games    ADD COLUMN lobby_warned_at REAL;
ALTER TABLE mc_games         ADD COLUMN lobby_warned_at REAL;
