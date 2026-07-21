-- Economy sinks round 3, stage 1 (docs/plans/economy-sinks-round-3.md).
--
-- 1) Widen the econ_rentals perk CHECK for the perks this round adds
--    (`voice_style` stage 3, `emoji` stage 4) — the SQLite table rebuild, done
--    ONCE here so later stages don't rebuild again.
-- 2) Retire `gift_color`: a gift is now the BASE perk kind rented with
--    `beneficiary_id` != `user_id` (the schema always supported this — the
--    live-rental unique index is keyed on the beneficiary, entitlements are
--    beneficiary-based, and leave-cleanup already covers both sides). Live and
--    historical gift_color rows are rewritten to role_color in the copy; their
--    beneficiary already differs from the owner, so the partial unique index
--    cannot collide with the owner's own role_color rental.
-- 3) `econ_streaks.shields` rides along (stage 2, prepaid streak shield) so
--    that table isn't touched twice: 0/1 shields held, consumed by the login
--    evaluator when a reset would land.
--
-- Columns carried over: everything through migration 077 (ended_at from 066,
-- catalog_icon_id from 077).

CREATE TABLE econ_rentals_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    user_id              INTEGER NOT NULL,
    perk                 TEXT    NOT NULL CHECK (perk IN
                             ('role_color', 'role_name', 'role_icon',
                              'role_gradient', 'voice_style', 'emoji')),
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
SELECT id, guild_id, user_id,
       CASE perk WHEN 'gift_color' THEN 'role_color' ELSE perk END,
       state, price, started_at, next_bill_at,
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

ALTER TABLE econ_streaks ADD COLUMN shields INTEGER NOT NULL DEFAULT 0;
