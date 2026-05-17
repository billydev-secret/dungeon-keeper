-- Migration 024: whisper UX v2 — soft-delete column for new inbox semantics
-- Data migration of legacy 'hidden' state -> deleted_at is deferred to a
-- later migration so this step stays purely additive.

ALTER TABLE whispers ADD COLUMN deleted_at REAL;

CREATE INDEX IF NOT EXISTS idx_whispers_sender_active
    ON whispers (guild_id, sender_id, deleted_at);
