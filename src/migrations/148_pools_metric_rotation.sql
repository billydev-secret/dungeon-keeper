-- Pools — rotate the market through a roster of metrics
-- (docs/plans/pools-metric-rotation.md).
--
-- Until now the market bet one hardcoded metric: the day's net change in
-- the economy. Rotation draws a different metric each guild-local day, so
-- the round has to record WHICH metric it was.
--
-- That column is not decoration. A pools round settles by RECOMPUTING its
-- outcome from history — that is what lets a round missed by hours still
-- settle against the line members bet into (migration 140). Recomputing
-- needs to know what to measure, so a round row that did not name its
-- metric would become unsettleable the moment the draw moved on.
--
-- The DEFAULT is what makes this migration safe on a live table: the
-- rounds already on disk were all bet on the economy's net change, and the
-- backfill gives them exactly that key, so they settle and render after
-- the migration precisely as they did before it.

ALTER TABLE casino_pools_rounds
    ADD COLUMN metric TEXT NOT NULL DEFAULT 'economy_net';
