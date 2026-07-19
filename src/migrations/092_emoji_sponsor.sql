-- Emoji sponsorship (economy sinks round 3, stage 4 —
-- docs/plans/economy-sinks-round-3.md).
--
-- A member pays weekly to keep a custom emoji in the server. Submission is
-- the econ_qotd_submissions shape (money taken at submit; deny/cancel/expiry
-- refund exactly-once via the refunded_at predicate), but approval graduates
-- the row into an ordinary econ_rentals row (perk = 'emoji', already in the
-- 091 CHECK) — billing, grace, and lapse ride the existing rental engine,
-- and a lapse deletes the emoji.
--
-- States: pending -> approved -> live, or pending -> denied / cancelled /
-- expired (all three refund). 'approved' is the claimed-but-not-uploaded
-- window (the mod approved; the bot upload is the post-commit side effect);
-- 'live' means the emoji exists and the rental is running. image_path is a
-- managed file under <db dir>/econ_emoji/<guild_id>/ so the dashboard can
-- preview it and a re-upload after suspension could re-read it.

CREATE TABLE IF NOT EXISTS econ_emoji_submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    name            TEXT    NOT NULL,
    image_path      TEXT    NOT NULL,
    animated        INTEGER NOT NULL DEFAULT 0,
    state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending','approved','live',
                                             'denied','cancelled','expired')),
    price           INTEGER NOT NULL DEFAULT 0,
    deny_reason     TEXT    NOT NULL DEFAULT '',
    emoji_id        INTEGER,
    rental_id       INTEGER,
    resolver_id     INTEGER,
    refunded_at     REAL,
    created_at      REAL    NOT NULL,
    resolved_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_econ_emoji_sub_state
    ON econ_emoji_submissions (guild_id, state, created_at);

-- One submission in flight per member (pending/approved/live all hold a slot
-- or a queue place); terminal rows free the member to sponsor again.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_emoji_sub_open
    ON econ_emoji_submissions (guild_id, user_id)
    WHERE state IN ('pending', 'approved', 'live');

-- One live claim per emoji name per guild, matching Discord's own
-- unique-name rule, so two members can't queue the same name.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_emoji_sub_name
    ON econ_emoji_submissions (guild_id, name)
    WHERE state IN ('pending', 'approved', 'live');
