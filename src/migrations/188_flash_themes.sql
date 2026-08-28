-- Flash Themes — a paid, mod-approved themed day in one channel.
--
-- A member pays to name the day's theme; a mod approves it; the approved
-- themes queue up and the hourly economy loop runs the oldest one whenever
-- the channel is free. Going live posts ONE card in `theme_channel_id` and
-- pins it — the announcement and the pin are the same message, not two — and
-- the sweep unpins it when the window is up.
--
-- The fourth consumer of economy_submission_store's shared mechanics
-- (migrations 090 sponsored QOTD, 092 emoji, 108 Pin of the Day), so the
-- column vocabulary below is deliberately theirs: `state`, `price`,
-- `refunded_at`, `deny_reason`, `card_*`, `resolver_id`, `resolved_at`.
--
-- Money is taken at SUBMIT (a free queue invites spam), so `denied` and a
-- `pending` that `expired` both owe a refund; a theme that actually RAN does
-- not — the member got their day, the same call Pin of the Day makes.
-- `refunded_at` is the exactly-once guard, not a flag the caller sets.
--
-- States:
--   pending --approve--> approved --(slot free)--> live --(window)--> expired
--      |                     |
--      ├--deny----> denied   └--withdraw--> denied   (both refund)
--      └--expire--> expired                          (refunds; pending only)

CREATE TABLE IF NOT EXISTS econ_theme_submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    -- What the buyer picked: a short name for the day, and a couple of lines
    -- telling members what to actually post. Two fields because a bare name
    -- rarely tells anyone what to do with it.
    title            TEXT    NOT NULL,
    blurb            TEXT    NOT NULL DEFAULT '',
    state            TEXT    NOT NULL DEFAULT 'pending'
                             CHECK (state IN
                                 ('pending','approved','live','denied','expired')),
    price            INTEGER NOT NULL DEFAULT 0,
    deny_reason      TEXT    NOT NULL DEFAULT '',
    -- The mod-approval card (in the bank channel).
    card_channel_id  INTEGER NOT NULL DEFAULT 0,
    card_message_id  INTEGER NOT NULL DEFAULT 0,
    -- The live announcement, which is also the pinned message.
    theme_channel_id INTEGER NOT NULL DEFAULT 0,
    theme_message_id INTEGER NOT NULL DEFAULT 0,
    went_live_at     REAL,
    expires_at       REAL,
    resolver_id      INTEGER,
    refunded_at      REAL,
    created_at       REAL    NOT NULL,
    resolved_at      REAL
);

-- The approval queue, the promote-the-next-one read and the expiry sweep, all
-- oldest-first.
CREATE INDEX IF NOT EXISTS idx_econ_theme_sub_state
    ON econ_theme_submissions (guild_id, state, created_at);

-- One submission in flight per member: you can't buy the next five themed
-- days. Terminal rows (denied/expired) are excluded so a member can buy again
-- once their theme has had its day.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_theme_sub_open
    ON econ_theme_submissions (guild_id, user_id)
    WHERE state IN ('pending', 'approved', 'live');

-- At most one LIVE theme per guild. Unlike Pin of the Day there is no
-- supersede path — a theme is promoted only when the channel is already free,
-- so this is the invariant itself and not just a backstop.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_theme_sub_live
    ON econ_theme_submissions (guild_id)
    WHERE state = 'live';
