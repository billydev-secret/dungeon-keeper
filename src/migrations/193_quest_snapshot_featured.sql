-- Freeze the featured-room pin alongside the pool it draws from.
--
-- The daily board reserves one slot for a quest belonging to whichever room
-- the feature rotation has open today. The *blocked* kinds were already
-- captured at freeze time, but the *featured* candidates were re-derived from
-- live rotation config on every board read — so an admin editing a room's
-- quest kinds, un-ticking "in rotation", or changing rooms-per-day at noon
-- silently moved the reserved slot for everyone who reloaded, and could push
-- a quest a member had already made progress on off their board.
--
-- assigned_board_ids promises a board that is "stable within the period".
-- Freezing the candidate ids next to the pool is what makes that true of the
-- pin as well. Empty means "no featured pin this period", which is also what
-- every row frozen before this column existed reads as -- the safe default,
-- and the pre-rotation behaviour.
ALTER TABLE econ_quest_pool_snapshots
    ADD COLUMN featured_json TEXT NOT NULL DEFAULT '';
