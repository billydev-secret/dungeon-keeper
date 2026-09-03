-- Migration 203: record which roles Dungeon Keeper made, and which it adopted
-- (2026-09-03, docs/plans/role-autocreate-round-2.md direction (d)).
--
-- Until now the bot could not tell you which roles it created. Every state the
-- dashboard showed was *inferred* from "the stored id still resolves" and "a
-- role of this name exists", which is why a second guild inheriting a
-- `guild_id = 0` dial could be told a role it never had was deleted, and why a
-- dial reading "(none)" could not be told apart from one an unrelated whole-form
-- save had cleared. This table is the fact behind those inferences: one row per
-- (guild, dial), written by `core/role_provision` at the moment it creates or
-- adopts, and by nothing else.
--
-- `origin` is 'created' (the bot made this role) or 'adopted' (the bot pointed
-- itself at a role the guild already had). The distinction is what makes
-- "stop managing" safe to offer: the bot stops pointing at a role it never
-- owned, and leaves it in the server either way.
--
-- **This table holds no per-user data** and therefore needs no
-- docs/data_register.md row — the same call migration 200 records for
-- `birthday_channels`. Every column names a guild, a dial or a role, never a
-- member. The acting admin is deliberately *not* stored: `write_audit` already
-- records who changed a role dial from the dashboard, and duplicating a member
-- id here would turn a table of server configuration into personal data with
-- an erasure obligation, for no information anybody lacks.

CREATE TABLE IF NOT EXISTS bot_managed_roles (
    guild_id    INTEGER NOT NULL,
    role_key    TEXT    NOT NULL,   -- registry key, e.g. 'welcome_ping_role_id'
    role_id     INTEGER NOT NULL,
    origin      TEXT    NOT NULL,   -- 'created' | 'adopted'
    recorded_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, role_key)
);

-- The roster page reads a whole guild at once.
CREATE INDEX IF NOT EXISTS idx_bot_managed_roles_guild
    ON bot_managed_roles (guild_id);
