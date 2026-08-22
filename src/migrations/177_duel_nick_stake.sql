-- Nickname stakes stop being inferred from `stakes_text IS NULL`.
--
-- Every duel/group game decided "does the winner get to rename the loser?" by
-- checking whether stakes_text was empty. That made the rename mutually
-- exclusive with a coin wager and with any custom stakes text — so a game
-- staked as "24 hour nickname change" plus 500 coins silently offered nobody a
-- rename button (pressure_games 41, 2026-08-22 04:30). Every combination is
-- now legal, carried by an explicit flag.
--
-- Backfill reproduces the old inference exactly, so games already on disk keep
-- the mode they were played under.

ALTER TABLE pressure_games   ADD COLUMN nick_stake INTEGER NOT NULL DEFAULT 0;
ALTER TABLE quickdraw_games  ADD COLUMN nick_stake INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hot_potato_games ADD COLUMN nick_stake INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hp_group_games   ADD COLUMN nick_stake INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chicken_games    ADD COLUMN nick_stake INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mc_games         ADD COLUMN nick_stake INTEGER NOT NULL DEFAULT 0;

UPDATE pressure_games   SET nick_stake = 1 WHERE stakes_text IS NULL;
UPDATE quickdraw_games  SET nick_stake = 1 WHERE stakes_text IS NULL;
UPDATE hot_potato_games SET nick_stake = 1 WHERE stakes_text IS NULL;
UPDATE hp_group_games   SET nick_stake = 1 WHERE stakes_text IS NULL;
UPDATE chicken_games    SET nick_stake = 1 WHERE stakes_text IS NULL;
UPDATE mc_games         SET nick_stake = 1 WHERE stakes_text IS NULL;

-- Per-guild, per-game cap on how many challenges one person can open an hour.
-- Was a hardcoded 3 in memory, which stopped the most engaged player in the
-- room twice on game night. 0 = no limit.
ALTER TABLE duel_config ADD COLUMN challenge_limit_per_hour INTEGER NOT NULL DEFAULT 30;
