-- Intake cards: one per newcomer, posted to greeter chat on join, tracking
-- the welcome procedure as ticked steps (see intake_service). The open row
-- (resolved_at IS NULL) dedupes joins/rejoins to a single live card; resolving
-- records how the intake ended (completed / dismissed / left / banned) and,
-- for completions, who posted the completion code (resolved_by).
CREATE TABLE IF NOT EXISTS intake_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    nudged_at REAL,
    resolved_at REAL,
    resolved_by INTEGER,
    resolution TEXT
);

-- One open card per (guild, member): the dedup gate join/message hooks rely on.
CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_cards_open
    ON intake_cards (guild_id, user_id)
    WHERE resolved_at IS NULL;

-- Checklist steps, snapshotted from guild config at card creation so editing
-- the configured step list never mutates in-flight cards. auto_kind '' means
-- a manual step (welcomer ticks a button); 'greeted' / 'verified' /
-- 'role_gained' steps are ticked by event hooks (role_gained matches
-- auto_role_id). done_by 0 = ticked automatically. skipped is stamped on
-- still-unticked steps when the completion code closes the card.
CREATE TABLE IF NOT EXISTS intake_card_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    step_key TEXT NOT NULL,
    label TEXT NOT NULL,
    auto_kind TEXT NOT NULL DEFAULT '',
    auto_role_id INTEGER NOT NULL DEFAULT 0,
    done_at REAL,
    done_by INTEGER,
    skipped INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_card_steps_key
    ON intake_card_steps (card_id, step_key);
