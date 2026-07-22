-- New rentable perk: `role_holographic` — Discord's holographic role preset
-- (the fixed three-colour shimmer set via `tertiary_colour`, distinct from the
-- member-picked two-colour `role_gradient`). SQLite can't ALTER a CHECK in
-- place, so widen the `econ_rentals.perk` constraint with a table rebuild
-- (same shape as migration 091). Every column and index is carried across
-- unchanged; only the CHECK list gains `'role_holographic'`.
--
-- No per-guild price is seeded: the flat price falls back to the EconSettings
-- default (`price_role_holographic` = 300), so the perk lists live at 300 on
-- every guild until an admin re-prices it on the Sinks page.

CREATE TABLE econ_rentals_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    user_id              INTEGER NOT NULL,
    perk                 TEXT    NOT NULL CHECK (perk IN
                             ('role_color', 'role_name', 'role_icon',
                              'role_gradient', 'role_holographic',
                              'voice_style', 'emoji')),
    state                TEXT    NOT NULL CHECK (state IN
                             ('active', 'grace', 'lapsed', 'cancelled')),
    price                INTEGER NOT NULL,
    started_at           REAL    NOT NULL,
    next_bill_at         REAL    NOT NULL,
    grace_since          REAL,
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    suspended            INTEGER NOT NULL DEFAULT 0,
    suspended_since      REAL,
    beneficiary_id       INTEGER NOT NULL,
    meta                 TEXT,
    created_at           REAL    NOT NULL,
    ended_at             REAL,
    catalog_icon_id      INTEGER
);

INSERT INTO econ_rentals_new
    (id, guild_id, user_id, perk, state, price, started_at, next_bill_at,
     grace_since, cancel_at_period_end, suspended, suspended_since,
     beneficiary_id, meta, created_at, ended_at, catalog_icon_id)
SELECT id, guild_id, user_id, perk, state, price, started_at, next_bill_at,
       grace_since, cancel_at_period_end, suspended, suspended_since,
       beneficiary_id, meta, created_at, ended_at, catalog_icon_id
FROM econ_rentals;

DROP TABLE econ_rentals;
ALTER TABLE econ_rentals_new RENAME TO econ_rentals;

CREATE INDEX IF NOT EXISTS idx_econ_rentals_billing
    ON econ_rentals (guild_id, state, next_bill_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_rentals_live
    ON econ_rentals (guild_id, user_id, perk, beneficiary_id)
    WHERE state IN ('active', 'grace');
