-- Paired board quests: two active quests of the same cadence sharing a
-- non-empty pair_tag are drawn as a bundle — when the personal board picks
-- one, the other replaces the remaining slot (quests.apply_pair_bundles).
-- Built for producer/consumer game quests ("submit a Guess Who round" +
-- "play a Guess Who round") so hosts and players are prompted together.
-- A tag shared by anything other than exactly two active quests is inert.
ALTER TABLE econ_quests ADD COLUMN pair_tag TEXT NOT NULL DEFAULT '';
