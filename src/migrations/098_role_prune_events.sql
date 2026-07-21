-- Durable ledger of role removals performed by automated strip mechanisms
-- (today: inactivity_prune_service). Lets the grant-audit panel classify a
-- missing role without recomputing activity history, and lets a mod's
-- later re-grant close the loop explicitly via restored_at.
CREATE TABLE IF NOT EXISTS role_prune_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'inactivity_prune',
    pruned_at REAL NOT NULL,
    restored_at REAL
);

CREATE INDEX IF NOT EXISTS idx_role_prune_events_open
    ON role_prune_events (guild_id, role_id, restored_at);

CREATE INDEX IF NOT EXISTS idx_role_prune_events_user
    ON role_prune_events (guild_id, user_id, role_id);
