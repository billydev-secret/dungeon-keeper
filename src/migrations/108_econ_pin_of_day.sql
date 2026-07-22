-- Pin of the Day (plan: docs/plans/pin-of-the-day.md).
--
-- A member pays to pin a short message; a mod approves it; the bot posts a
-- "Pinned by @X" card in the pin channel and pins it for 24h, then auto-unpins.
-- Modelled on econ_qotd_submissions (migration 090): a small state machine with
-- partial unique indexes doing the concurrency work.
--
-- Money is taken at SUBMIT (a free queue invites spam), so `denied` and a
-- `pending` that `expired` both owe a refund; a pin that went `live` does NOT —
-- they got their time up. `refunded_at` is the exactly-once guard, not a flag
-- the caller sets.
--
-- States:
--   pending --approve--> live --(24h)--> expired
--      |                   |
--      |                   +--replaced by a newer approval--> superseded
--      ├--deny----> denied            (refund)
--      └--expire--> expired           (refund; pending only)

CREATE TABLE IF NOT EXISTS econ_pin_submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    message         TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN
                                ('pending','live','denied','expired','superseded')),
    price           INTEGER NOT NULL DEFAULT 0,
    deny_reason     TEXT    NOT NULL DEFAULT '',
    -- The mod-approval card (in the bank channel).
    card_channel_id INTEGER NOT NULL DEFAULT 0,
    card_message_id INTEGER NOT NULL DEFAULT 0,
    -- The live pinned card (in the pin channel).
    pin_channel_id  INTEGER NOT NULL DEFAULT 0,
    pin_message_id  INTEGER NOT NULL DEFAULT 0,
    went_live_at    REAL,
    expires_at      REAL,
    resolver_id     INTEGER,
    refunded_at     REAL,
    created_at      REAL    NOT NULL,
    resolved_at     REAL
);

-- The approval queue and the live-expiry sweep, both oldest-first.
CREATE INDEX IF NOT EXISTS idx_econ_pin_sub_state
    ON econ_pin_submissions (guild_id, state, created_at);

-- One submission in flight per member: you can't spam the mod queue by buying
-- ten slots. Terminal rows (denied/expired/superseded) are excluded so a member
-- can pin again once their last one ran its course.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_pin_sub_open
    ON econ_pin_submissions (guild_id, user_id)
    WHERE state IN ('pending', 'live');

-- At most one LIVE pin per guild: go_live supersedes the prior live row in the
-- same transaction before promoting the new one, so this can never collide in
-- normal flow — it is the backstop that keeps the invariant true.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_pin_sub_live
    ON econ_pin_submissions (guild_id)
    WHERE state = 'live';
