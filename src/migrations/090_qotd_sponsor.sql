-- Sponsor-a-QOTD (plan: economy-sinks-round-2 stage 3).
--
-- A member pays to put a question in front of the server; a mod approves it
-- before it can run. Modelled on econ_quest_claims: a small state machine
-- with partial unique indexes doing the concurrency work, rather than
-- application-level locking.
--
-- States: pending -> approved -> posted, or pending -> denied / expired.
-- The money is taken at SUBMIT (a free queue invites spam), so denied and
-- expired both owe a refund; refunded_at is the exactly-once guard, not a
-- flag we trust the caller to set.
--
-- econ_qotd.sponsor_user_id is deliberately a NEW column rather than a reuse
-- of posted_by: posted_by is the mod who ran /qotd post and stays that way,
-- since the two are different people and both matter for an audit.

CREATE TABLE IF NOT EXISTS econ_qotd_submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    question        TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending','approved','posted','denied','expired')),
    price           INTEGER NOT NULL DEFAULT 0,
    deny_reason     TEXT    NOT NULL DEFAULT '',
    card_channel_id INTEGER NOT NULL DEFAULT 0,
    card_message_id INTEGER NOT NULL DEFAULT 0,
    qotd_id         INTEGER,
    resolver_id     INTEGER,
    refunded_at     REAL,
    created_at      REAL    NOT NULL,
    resolved_at     REAL,
    posted_at       REAL
);

-- The approval queue and the ready-to-post queue, both oldest-first.
CREATE INDEX IF NOT EXISTS idx_econ_qotd_sub_state
    ON econ_qotd_submissions (guild_id, state, created_at);

-- One question in flight per member: you can't spam the mod queue by buying
-- ten slots at once, and you can't hold the ready queue hostage either.
-- Terminal rows (posted/denied/expired) are excluded so a member can sponsor
-- again once their last one has run its course.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_qotd_sub_open
    ON econ_qotd_submissions (guild_id, user_id)
    WHERE state IN ('pending', 'approved');

ALTER TABLE econ_qotd ADD COLUMN sponsor_user_id INTEGER NOT NULL DEFAULT 0;
