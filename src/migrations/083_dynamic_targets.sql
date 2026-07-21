-- Dynamic personal targets (plan: quest-variety, "Dynamic targets").
--
-- A counted quest with a target band now sizes each member's target from
-- their OWN trailing activity (median of the last periods of that kind
-- × 1.15, clamped to the author's band) instead of the pure Gaussian draw —
-- the Gaussian remains the no-history fallback. The resolved target is
-- computed once per (quest, member, period) at first touch and stored here
-- so it never moves mid-period and the wallet shows exactly what the fire
-- path enforces. NULL = not resolved yet (or a pre-migration row: those
-- periods keep resolving on their next touch).

ALTER TABLE econ_quest_progress ADD COLUMN target INTEGER;
