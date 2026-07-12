-- Economy quests — the quest library, per-member claims, and community goals.
--
-- `econ_quests` is the guild's quest library: dailies/weeklies (claimable
-- once per guild-local day / ISO week) and community goals (one guild-wide
-- objective). `signoff` flags a quest whose claims need manager approval;
-- `rotate_tag` groups a pool the loop cycles on the day/week roll.
--
-- `econ_quest_claims` is the money-critical state machine. A claim's `period`
-- is the guild-local day for dailies, the ISO week for weeklies, and 'once'
-- for community. The two partial unique indexes are the race anchors: at most
-- one 'pending' and at most one 'paid' row per (quest, user, period). Instant
-- quests insert 'paid' directly; sign-off quests insert 'pending' and the
-- approve transition moves it to 'paid' (the paid index is the double-pay
-- backstop at approve time). 'denied'/'expired' rows accumulate freely — they
-- are the re-claimable deny history.
--
-- `econ_community_progress` tracks a community quest's running total;
-- `completed_at` is set exactly once when progress first crosses the target
-- and `settled_at` only after the payout sweep finishes. `econ_community_payouts`
-- reserves one row per (quest, member) before crediting so a crashed/replayed
-- settle pays only the members it missed (wellness-scheduler pattern).
--
-- `econ_day_marks.last_iso_week` lets the loop detect the ISO-week roll for
-- weekly rotation and the community settlement sweep (the loop fills it).

CREATE TABLE IF NOT EXISTS econ_quests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    title             TEXT    NOT NULL,
    description       TEXT    NOT NULL DEFAULT '',
    qtype             TEXT    NOT NULL CHECK (qtype IN ('daily', 'weekly', 'community')),
    reward            INTEGER NOT NULL DEFAULT 0,
    signoff           INTEGER NOT NULL DEFAULT 0,
    criteria          TEXT    NOT NULL DEFAULT '',
    starts_at         REAL,
    ends_at           REAL,
    active            INTEGER NOT NULL DEFAULT 0,
    rotate_tag        TEXT    NOT NULL DEFAULT '',
    community_target  INTEGER,
    created_by        INTEGER,
    created_at        REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_econ_quests_guild_active
    ON econ_quests (guild_id, active, qtype);

CREATE TABLE IF NOT EXISTS econ_quest_claims (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    quest_id         INTEGER NOT NULL,
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    period           TEXT    NOT NULL,        -- "YYYY-MM-DD" | "YYYY-Www" | "once"
    state            TEXT    NOT NULL CHECK (state IN ('pending', 'paid', 'denied', 'expired')),
    created_at       REAL    NOT NULL,
    resolved_at      REAL,
    resolver_id      INTEGER,
    deny_reason      TEXT,
    card_channel_id  INTEGER,
    card_message_id  INTEGER
);

-- Race anchors: at most one pending and at most one paid per period.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_quest_claims_pending
    ON econ_quest_claims (quest_id, user_id, period) WHERE state = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_quest_claims_paid
    ON econ_quest_claims (quest_id, user_id, period) WHERE state = 'paid';
CREATE INDEX IF NOT EXISTS idx_econ_quest_claims_lookup
    ON econ_quest_claims (quest_id, user_id, state);

CREATE TABLE IF NOT EXISTS econ_community_progress (
    quest_id      INTEGER PRIMARY KEY,
    current       INTEGER NOT NULL DEFAULT 0,
    completed_at  REAL,
    settled_at    REAL
);

CREATE TABLE IF NOT EXISTS econ_community_payouts (
    quest_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    PRIMARY KEY (quest_id, user_id)
);

-- Loop's ISO-week roll detector (weekly rotation + community settle sweep).
ALTER TABLE econ_day_marks ADD COLUMN last_iso_week TEXT;
