-- Casino fancy round (docs/plans/casino-fancy-round.md): progressive
-- jackpot + member/weekly play stats. All bookkeeping over money the
-- ledger already recorded — the pot tracks a slice of past losses (paid
-- out later as a casino_payout), stats tables are bounded upserts (per
-- member, per member-week), never a per-play log.

CREATE TABLE IF NOT EXISTS casino_jackpot (
    guild_id       INTEGER PRIMARY KEY,
    pot            INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL    NOT NULL,
    last_winner_id INTEGER,
    last_amount    INTEGER,
    last_won_at    REAL
);

CREATE TABLE IF NOT EXISTS casino_member_stats (
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    wagered          INTEGER NOT NULL DEFAULT 0,
    returned         INTEGER NOT NULL DEFAULT 0,
    plays            INTEGER NOT NULL DEFAULT 0,
    wins             INTEGER NOT NULL DEFAULT 0,
    biggest_win      INTEGER NOT NULL DEFAULT 0,  -- largest single total return
    biggest_win_game TEXT    NOT NULL DEFAULT '',
    streak           INTEGER NOT NULL DEFAULT 0,  -- +n win run, -n loss run
    best_streak      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- "Night at the Tables" leaderboard block reads the current ISO week's
-- rows (iso_week format matches quests.iso_week_for: "YYYY-Www").
CREATE TABLE IF NOT EXISTS casino_weekly (
    guild_id          INTEGER NOT NULL,
    iso_week          TEXT    NOT NULL,
    user_id           INTEGER NOT NULL,
    wagered           INTEGER NOT NULL DEFAULT 0,
    won               INTEGER NOT NULL DEFAULT 0,
    biggest_win       INTEGER NOT NULL DEFAULT 0,
    biggest_mult_x100 INTEGER NOT NULL DEFAULT 0,  -- payout/stake ×100
    PRIMARY KEY (guild_id, iso_week, user_id)
);
