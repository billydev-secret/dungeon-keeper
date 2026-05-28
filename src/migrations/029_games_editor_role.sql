-- 029_games_editor_role.sql
-- Stores the optional Discord role that grants full Games section access on the dashboard (Game Host Role).

CREATE TABLE IF NOT EXISTS games_editor_role (
    guild_id    INTEGER PRIMARY KEY,
    role_id     INTEGER NOT NULL,
    set_by      INTEGER NOT NULL DEFAULT 0,
    set_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
