-- 095_rules_ledger.sql
-- Rules Watch ledger: records concrete, citable acts. NOT a detector.
--
-- Distinct from `rules_events` (030) in both purpose and tone. A rules_event is
-- the guard model's opinion that a message may violate a rule. A ledger row is
-- the observation that a specific, narrowly-defined thing was said — it makes no
-- claim about intent and accuses nobody. It exists so that when a human is
-- already reviewing someone, the prior acts are on record with dates.
--
-- Privacy: stores the matched phrase and a capped excerpt, never full content.

CREATE TABLE IF NOT EXISTS rules_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    kind            TEXT    NOT NULL,  -- 'dm_consent' | 'cross_platform'
    message_id      INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    author_id       INTEGER NOT NULL,
    target_id       INTEGER,           -- NULL when the addressee is ambiguous
    target_confidence TEXT,
    -- What actually matched, for citation in review. Never the full message.
    matched_phrase  TEXT,
    excerpt         TEXT,
    -- cross_platform only: which platform was named
    platform        TEXT,
    detected_at     REAL    NOT NULL,
    UNIQUE (message_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_rules_ledger_guild_kind_ts
    ON rules_ledger (guild_id, kind, detected_at);

CREATE INDEX IF NOT EXISTS idx_rules_ledger_author
    ON rules_ledger (guild_id, author_id, detected_at);
