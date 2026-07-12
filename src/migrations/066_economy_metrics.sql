-- Economy — weekly metrics rollup and rental end-of-life stamping (spec §9).
--
-- `econ_metrics_weekly` is one immutable row per (guild, closed ISO week),
-- computed at the guild-local week roll for the week that JUST closed. The
-- (guild_id, iso_week) primary key is the idempotency anchor: the rollup is
-- written INSERT OR IGNORE so a loop crash/replay recomputes nothing (see
-- economy_metrics_service.compute_weekly_rollup, which returns None on replay).
-- All income/mint/burn figures are derived from `econ_ledger` inside the week's
-- epoch bounds; transfers are excluded both directions (they move currency, they
-- do not mint or burn it). `faucet_mix` is a JSON object of minted-share by
-- faucet group (logins/activity/quests/games/grants), each a 0-1 fraction.
--
-- `ended_at` is added to `econ_rentals` so the rollup can count churn: every
-- rental-terminating path (billing revoke / period-end cancel, grace-immediate
-- cancel, member-leave cleanup) stamps it, and `rentals_ended` counts rows whose
-- `ended_at` falls inside the closed week. NULL for still-live rentals.

CREATE TABLE IF NOT EXISTS econ_metrics_weekly (
    guild_id        INTEGER NOT NULL,
    iso_week        TEXT    NOT NULL,           -- "YYYY-Www", the CLOSED week
    median_income   REAL,                       -- median weekly income over earners
    p90_income      REAL,                       -- 90th pct (nearest-rank) income
    active_members  INTEGER,                    -- active in last 30 days at rollup
    earners         INTEGER,                    -- members with income > 0 in week
    minted          INTEGER,                    -- sum credits, excl. transfer_in
    burned          INTEGER,                    -- |sum debits|, excl. transfer_out
    faucet_mix      TEXT,                        -- JSON {group: share 0-1}
    rental_holders  INTEGER,                    -- distinct live-rental beneficiaries
    rentals_live    INTEGER,                    -- live rental count at rollup
    rentals_ended   INTEGER,                    -- rentals ended within the week
    streaks_7plus   INTEGER,                    -- streak >= 7 seen this week
    grace_used      INTEGER,                    -- grace consumed this week
    computed_at     REAL,
    PRIMARY KEY (guild_id, iso_week)
);

ALTER TABLE econ_rentals ADD COLUMN ended_at REAL;
