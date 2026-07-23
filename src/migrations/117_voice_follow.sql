-- Directed "voice follow" capture.
--
-- When a member JOINS a voice channel that another member is already sitting
-- in, that join is a directed attention signal: from_user_id (the joiner)
-- "came to where to_user_id already was". Unlike the symmetric voice_partner
-- quest trigger — which fires for everyone co-present at an earning tick —
-- join order supplies direction for free, which is exactly what makes this
-- usable for the one-sided / unreciprocated-attention report (A keeps showing
-- up in B's channel; B never seeks A out).
--
-- Two tables, mirroring user_interactions / user_interactions_log:
--   voice_follow      – aggregate weight per ordered pair, for fast report lookups
--   voice_follow_log  – timestamped events, for burst / escalation analysis
--
-- Noise guards live in code (voice_follow.py): joins into an empty channel are
-- not recorded, joins into a crowd are ignored (party, not pursuit), and rapid
-- leave/rejoin flapping into the same channel is debounced so one restless
-- session cannot inflate the weight.

CREATE TABLE IF NOT EXISTS voice_follow (
    guild_id     INTEGER NOT NULL,
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    weight       INTEGER NOT NULL DEFAULT 0,
    last_ts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, from_user_id, to_user_id)
);

CREATE TABLE IF NOT EXISTS voice_follow_log (
    guild_id     INTEGER NOT NULL,
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    ts           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_follow_log_guild_ts
    ON voice_follow_log (guild_id, ts);

-- Supports both the debounce lookup (pair + channel + recent ts) and the
-- report's per-pair history scan.
CREATE INDEX IF NOT EXISTS idx_voice_follow_log_pair
    ON voice_follow_log (guild_id, from_user_id, to_user_id, ts);
