-- Chat Revive session-gap ("conversation") model.
--
-- The old rhythm cache stored per-band inter-*message* gap stats
-- (median_gap/p90_gap/gap_count) and fired at max(4 x median, p90). That base
-- is dominated by tiny intra-burst gaps, so busy channels got a ~4-minute
-- hair-trigger. The new model segments each band into conversations and fires
-- at a high quantile of the between-conversation gaps (see chat_revive/logic.py
-- compute_band_profiles / _session_threshold).
--
-- revive_channel_rhythm is a pure recomputable cache (DELETE+INSERT every
-- refresh, 6h TTL), so we just drop and recreate it with the new columns; the
-- first monitor tick per channel repopulates it within one TTL.

DROP TABLE IF EXISTS revive_channel_rhythm;

CREATE TABLE IF NOT EXISTS revive_channel_rhythm (
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    band            INTEGER NOT NULL,
    fire_threshold  REAL    NOT NULL,
    sessions_per_day REAL   NOT NULL,
    msgs_per_day    REAL    NOT NULL,
    session_count   INTEGER NOT NULL,
    computed_at     REAL    NOT NULL,
    PRIMARY KEY (guild_id, channel_id, band)
);

-- fire_multiplier is repurposed from "x the inter-message median" (default 4.0,
-- range 2-10) to a patience multiplier on the learned lull threshold (default
-- 1.0, range 0.5-3.0). Old per-channel values map to nothing meaningful under
-- the new base, so reset every configured channel to the neutral 1.0. New rows
-- always carry an explicit value from the dashboard, so the column's own
-- DEFAULT (still 4.0 in the table schema) is cosmetic and never read.
UPDATE revive_channel_config SET fire_multiplier = 1.0;
