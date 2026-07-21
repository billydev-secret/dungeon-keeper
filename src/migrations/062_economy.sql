-- Economy — per-guild soft currency wallets and an audit ledger.
--
-- `econ_wallets` holds one balance row per (guild, user); the CHECK keeps it
-- from ever going negative so a debit that would underflow fails at the SQL
-- layer as well as in the service. Every balance change writes an `econ_ledger`
-- row — signed amount (+credit, −debit), a free-text `kind` (stage 0 emits
-- only `grant`; later stages add login/conversion/etc.), the acting user, and a
-- JSON `meta` blob. `econ_notify_prefs` records users who muted balance-change
-- DMs. Per-guild scalar settings live in the shared `config` KV table under the
-- `econ_` prefix, not here.

CREATE TABLE IF NOT EXISTS econ_wallets (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    balance     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id),
    CHECK (balance >= 0)
);

CREATE TABLE IF NOT EXISTS econ_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,            -- signed: + credit, − debit
    kind        TEXT    NOT NULL,
    actor_id    INTEGER,                     -- NULL for system-driven changes
    meta        TEXT,                        -- optional JSON blob
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS econ_notify_prefs (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    muted       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_econ_ledger_user
    ON econ_ledger (guild_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_econ_ledger_guild
    ON econ_ledger (guild_id, created_at);
