-- Economy faucets — daily logins/streaks, XP conversion, QOTD, reaction-XP dedup.
--
-- `econ_logins` is the idempotency anchor for the daily login: one row per
-- (guild, user, guild-local day), INSERT OR IGNORE'd before any credit so
-- racing events pay at most once (`paid` records the total credited that day).
-- `econ_streaks` carries the streak state the login evaluator reads/writes;
-- `last_grace_day` anchors the rolling-7-day one-free-miss window.
-- `econ_conversions` records each nightly XP→currency conversion — the
-- fractional `remainder` carries to the next day's conversion.
-- `econ_day_marks` is the hourly loop's day-roll detector (one row per guild).
-- `econ_qotd` + `econ_qotd_rewards` track posted questions and the
-- once-per-member reward dedup. `xp_reaction_awards` dedups the
-- reaction_given XP source forever, so react/unreact can't farm.

CREATE TABLE IF NOT EXISTS econ_logins (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    local_day   TEXT    NOT NULL,            -- "YYYY-MM-DD", guild-local
    source      TEXT    NOT NULL,            -- "text" | "voice"
    paid        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, local_day)
);

CREATE TABLE IF NOT EXISTS econ_streaks (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    current_streak  INTEGER NOT NULL DEFAULT 0,
    longest_streak  INTEGER NOT NULL DEFAULT 0,
    last_login_day  TEXT,
    last_grace_day  TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS econ_conversions (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    local_day   TEXT    NOT NULL,
    xp          REAL    NOT NULL,
    coins       INTEGER NOT NULL,
    remainder   REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id, local_day)
);

CREATE TABLE IF NOT EXISTS econ_day_marks (
    guild_id        INTEGER PRIMARY KEY,
    last_local_day  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS econ_qotd (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    question    TEXT    NOT NULL,
    posted_by   INTEGER NOT NULL,
    local_day   TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS econ_qotd_rewards (
    qotd_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    PRIMARY KEY (qotd_id, user_id)
);

CREATE TABLE IF NOT EXISTS xp_reaction_awards (
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_econ_qotd_open
    ON econ_qotd (guild_id, channel_id, local_day);
