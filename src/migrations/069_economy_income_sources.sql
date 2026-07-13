-- Economy income sources — per-guild enable switches for the custom-coded
-- quest trigger hooks (quests.TRIGGER_KINDS).
--
-- Absent row = enabled (sources default ON so a new kind starts working
-- without a dashboard visit). A disabled source stops firing at the single
-- choke point (fire_trigger_quests) — quests referencing it stay in the
-- library untouched, they just never auto-complete until re-enabled.

CREATE TABLE IF NOT EXISTS econ_income_sources (
    guild_id    INTEGER NOT NULL,
    source      TEXT    NOT NULL,   -- a quests.TRIGGER_KINDS key
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (guild_id, source)
);
