-- Migration 012: veil audit log for moderator review

CREATE TABLE IF NOT EXISTS veil_audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    ts        REAL    NOT NULL,
    actor_id  INTEGER NOT NULL DEFAULT 0,
    action    TEXT    NOT NULL,
    round_id  INTEGER,
    details   TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_veil_audit_guild_ts
    ON veil_audit_log(guild_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_veil_audit_round
    ON veil_audit_log(round_id);
