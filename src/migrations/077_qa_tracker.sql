-- QA Tracker — volunteer testing crew with currency rewards.
--
-- `qa_tests` is one row per TESTING_QUEUE entry posted as an interactive
-- card (Pass / Fail / Blocked buttons). `status` is computed in qa_service
-- with precedence fail > blocked > pass > pending; 'archived' is set
-- explicitly from the dashboard and is terminal, never computed.
-- `verified_by`/`verified_at` stamp the first (earliest un-voided) passer.
-- The unique (guild_id, entry_key, commit_sha) index makes the post-commit
-- hook's insert idempotent across re-runs.
--
-- `qa_verdicts` holds one verdict per tester per test — the UNIQUE
-- constraint is the payment race-anchor (pay only when the INSERT lands;
-- a re-click is an UPDATE and never pays again). `paid_amount` records the
-- coins minted for this verdict (0 = unpaid: capped, disabled, or zero
-- reward). Voiding keeps the row for audit (voided_by/voided_at) and claws
-- the coins back via a `qa_void` ledger debit.

CREATE TABLE IF NOT EXISTS qa_tests (
    id             INTEGER PRIMARY KEY,
    guild_id       INTEGER NOT NULL,
    entry_key      TEXT    NOT NULL,
    title          TEXT    NOT NULL,
    body_md        TEXT    NOT NULL,
    commit_sha     TEXT,
    commit_subject TEXT,
    channel_id     INTEGER,
    message_id     INTEGER,
    thread_id      INTEGER,
    status         TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'passed', 'failed', 'blocked', 'archived')),
    verified_by    INTEGER,
    verified_at    TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_qa_tests_entry
    ON qa_tests (guild_id, entry_key, commit_sha);

CREATE INDEX IF NOT EXISTS idx_qa_tests_guild_status
    ON qa_tests (guild_id, status);

CREATE TABLE IF NOT EXISTS qa_verdicts (
    id          INTEGER PRIMARY KEY,
    test_id     INTEGER NOT NULL REFERENCES qa_tests(id),
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    verdict     TEXT    NOT NULL CHECK (verdict IN ('pass', 'fail', 'blocked')),
    note        TEXT,
    paid_amount INTEGER NOT NULL DEFAULT 0,
    voided_by   INTEGER,
    voided_at   TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE (test_id, user_id)
);
