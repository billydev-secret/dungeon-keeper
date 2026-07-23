-- Per-guild product names. The casino's name ("Golden Meadow") and the AI
-- assistant's name ("Billy-bot") were hardcoded in source, so every guild the
-- bot joins saw the home server's branding. Both now live on branding_config
-- alongside the accent color; NULL means "use the built-in default", so an
-- existing guild reads back exactly the text it has today.
--
-- branding_config is created lazily by branding_service._create_tables (a
-- guild that never saved branding has no table yet), so create it here first
-- with the same shape — the ALTERs below need it to exist. Re-running is safe:
-- CREATE ... IF NOT EXISTS is a no-op and the migration runner tolerates the
-- "duplicate column name" error from an already-applied ADD COLUMN.
CREATE TABLE IF NOT EXISTS branding_config (
    guild_id    INTEGER PRIMARY KEY,
    accent_mode TEXT NOT NULL DEFAULT 'avatar',
    accent_hex  INTEGER NOT NULL DEFAULT -1
);

ALTER TABLE branding_config ADD COLUMN casino_name TEXT;
ALTER TABLE branding_config ADD COLUMN assistant_name TEXT;
