-- Weekly raffle (economy sinks round 3, stage 5 —
-- docs/plans/economy-sinks-round-3.md).
--
-- Members buy week-scoped tickets (a pure burn — coins are never paid back
-- out); at the guild's ISO-week roll a weighted winner is drawn and receives
-- a free-perk-week voucher. The draws table's (guild, week) primary key is
-- the exactly-once claim: the draw row is inserted BEFORE any side effect
-- (DM, panel refresh), the scheduled-games pattern, so a crash-and-rerun of
-- the week roll can't draw twice.
--
-- Vouchers are deliberately generic (kind CHECK grows later if other prizes
-- appear). 'free_week' covers ONE rental debit — the next renewal or the
-- first week of a new rent — recorded as a 0-amount ledger row so the
-- register still narrates it. Unredeemed vouchers expire (expires_at swept
-- by the redemption check itself, no cron needed).

CREATE TABLE IF NOT EXISTS econ_raffle_tickets (
    guild_id  INTEGER NOT NULL,
    iso_week  TEXT    NOT NULL,              -- "YYYY-Www", guild-local
    user_id   INTEGER NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, iso_week, user_id)
);

CREATE TABLE IF NOT EXISTS econ_raffle_draws (
    guild_id   INTEGER NOT NULL,
    iso_week   TEXT    NOT NULL,             -- the week whose tickets were drawn
    winner_id  INTEGER,                      -- NULL = zero-ticket week, no winner
    tickets    INTEGER NOT NULL DEFAULT 0,   -- total tickets in the draw
    entrants   INTEGER NOT NULL DEFAULT 0,
    voucher_id INTEGER,
    drawn_at   REAL    NOT NULL,
    PRIMARY KEY (guild_id, iso_week)
);

CREATE TABLE IF NOT EXISTS econ_vouchers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('free_week')),
    state       TEXT    NOT NULL DEFAULT 'issued'
                        CHECK (state IN ('issued', 'redeemed', 'expired')),
    source      TEXT    NOT NULL DEFAULT '',  -- e.g. "raffle:2026-W29"
    created_at  REAL    NOT NULL,
    expires_at  REAL,
    redeemed_at REAL,
    rental_id   INTEGER                       -- the debit it covered
);

CREATE INDEX IF NOT EXISTS idx_econ_vouchers_lookup
    ON econ_vouchers (guild_id, user_id, state);
