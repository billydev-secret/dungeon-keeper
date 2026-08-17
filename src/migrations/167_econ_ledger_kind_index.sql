-- 167_econ_ledger_kind_index.sql
-- Survivor's pot math (survivor/logic.pot_totals) reads the ledger by
-- (guild_id, kind) on every public /survivor board render, and the existing
-- indexes (062: user- and time-oriented) leave that a guild-wide scan on the
-- repo's biggest money table. Kind-scoped reads are a general pattern —
-- economy metrics group by kind constantly — so the index earns its keep
-- beyond Survivor.
CREATE INDEX IF NOT EXISTS idx_econ_ledger_guild_kind
    ON econ_ledger (guild_id, kind);
