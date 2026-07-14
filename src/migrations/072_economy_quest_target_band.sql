-- Economy quests — per-member Gaussian target band (spec §4.6).
--
-- A counted quest can carry a target *band* instead of a fixed count: when
-- `0 < target_min < target_max`, each member's target for a period is drawn
-- from a Gaussian over [target_min, target_max], deterministic on
-- (user, quest, period) so it is stable all period and varies member to
-- member (quests.effective_target). 0/0 (the default) means "no band" — the
-- fixed `target_count` applies, so existing quests are unchanged.
--
-- This rides the per-user quest board: each cadence's active quests form a
-- pool and every member draws a personal subset each period
-- (quests.assigned_quest_ids), so the old single-active-daily slot rule is
-- retired in favour of a generous per-cadence pool cap.

ALTER TABLE econ_quests ADD COLUMN target_min INTEGER NOT NULL DEFAULT 0;
ALTER TABLE econ_quests ADD COLUMN target_max INTEGER NOT NULL DEFAULT 0;
