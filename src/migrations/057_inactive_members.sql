-- Inactive-channel holds.
--
-- Mirrors the `jails` table but for the softer "moved to the inactive channel"
-- flow: a member's roles are snapshotted and stripped, they're given the
-- @Inactive role (which can only see the shared inactive channel), and they
-- file a ticket to be reactivated. Kept separate from `jails` so the jail
-- expiry loop and /modinfo never misread an inactive hold as a jail.

CREATE TABLE IF NOT EXISTS inactive_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    moderator_id    INTEGER NOT NULL DEFAULT 0,
    reason          TEXT    NOT NULL DEFAULT '',
    stored_roles    TEXT    NOT NULL DEFAULT '[]',
    source          TEXT    NOT NULL DEFAULT 'command',
    created_at      REAL    NOT NULL,
    reactivated_at  REAL,
    reactivate_reason TEXT  NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_inactive_active
    ON inactive_members (guild_id, status) WHERE status = 'active';
