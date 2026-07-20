-- 096_pen_pals_blocks.sql
-- Pen Pals: "never match these two" exclusions.
--
-- Two sources feed one table:
--   * source='member' — a member's own self-service blocklist (directional:
--     one row per (blocker → blockee), managed via /penpals block).
--   * source='admin'  — a mod-enforced separation between two members,
--     normalized to (min_id, max_id) so there's exactly one row per pair,
--     managed on the Pen Pals dashboard.
--
-- Matching treats every row as symmetric: a pairing is excluded if ANY row
-- connects the two members in either direction, from either source. One
-- member blocking is enough; neither side learns who set it.

CREATE TABLE IF NOT EXISTS pen_pals_blocks (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    blocked_user_id INTEGER NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'member',  -- 'member' | 'admin'
    created_at      REAL    NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (guild_id, user_id, blocked_user_id, source)
);

-- The user_id direction is covered by the PK prefix (guild_id, user_id, …);
-- this index covers the reverse-direction lookup in the symmetry check.
CREATE INDEX IF NOT EXISTS idx_pen_pals_blocks_reverse
    ON pen_pals_blocks (guild_id, blocked_user_id);
