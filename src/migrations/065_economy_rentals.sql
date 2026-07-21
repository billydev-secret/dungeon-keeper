-- Economy — perk rentals (weekly auto-renew) and the desired personal-role state.
--
-- `econ_rentals` is the money-critical rental ledger. One row per rented perk;
-- `state` walks active → grace → lapsed/cancelled. `next_bill_at` is the weekly
-- anniversary tick — billing advances it from the *scheduled* time (not wall
-- clock) so anniversaries never drift when the loop runs late, and a multi-week
-- gap charges once and jumps to the next future anniversary. `grace_since`
-- anchors the 36h grace window after a failed debit; `cancel_at_period_end`
-- marks an owner cancel that runs out the paid week. `suspended` freezes billing
-- when a required guild feature (role icon / gradient) disappears mid-rental —
-- `suspended_since` records when so resume can push `next_bill_at` forward by
-- the frozen span (no charge while suspended). `price` is the snapshot charged
-- at rent time; renewals bill the current guild price (spec §6/§9 — prices are
-- per-guild tunable and take effect at the next anniversary) and refresh this.
--
-- `beneficiary_id` is who the perk applies to: the renter for self-perks, the
-- friend for gift_color. It is ALWAYS non-NULL (defaults to user_id) because the
-- partial unique index below is the race anchor for one live rental per
-- (guild, user, perk, beneficiary) and SQLite treats NULLs as distinct — a NULL
-- beneficiary would silently defeat the duplicate guard. Room perks are Stage 6;
-- a later migration can extend the perk CHECK.
--
-- `econ_personal_roles` is the DESIRED role state per member. The actual Discord
-- role is a projection recomputed from the member's active rentals — this table
-- holds what they've configured (name / color / gradient second color / icon),
-- so the projector can rebuild the role idempotently after a lapse or restart.

CREATE TABLE IF NOT EXISTS econ_rentals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    user_id              INTEGER NOT NULL,
    perk                 TEXT    NOT NULL CHECK (perk IN
                             ('role_color', 'role_name', 'role_icon',
                              'role_gradient', 'gift_color')),
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
    created_at           REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_econ_rentals_billing
    ON econ_rentals (guild_id, state, next_bill_at);

-- Race anchor: at most one LIVE rental per (guild, user, perk, beneficiary).
-- lapsed/cancelled rows accumulate freely (re-rent history) and never block.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_rentals_live
    ON econ_rentals (guild_id, user_id, perk, beneficiary_id)
    WHERE state IN ('active', 'grace');

CREATE TABLE IF NOT EXISTS econ_personal_roles (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    role_id     INTEGER,
    name        TEXT    NOT NULL DEFAULT '',
    color       INTEGER NOT NULL DEFAULT -1,   -- -1 = unset (use default)
    color2      INTEGER NOT NULL DEFAULT -1,   -- gradient second color, -1 = unset
    icon_path   TEXT    NOT NULL DEFAULT '',
    updated_at  REAL,
    PRIMARY KEY (guild_id, user_id)
);
