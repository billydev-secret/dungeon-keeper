-- 176_mahjong_prefs.sql
-- Meadow Mahjong assistance modes (docs/plans/mahjong-assist.md stage 2).
-- One row per member per guild: the assistance level they chose from the
-- /mahjong My Settings menu ('off' | 'target' | 'gap' | 'coach'). No row
-- means the guild default applies (config KV mahjong_assist_default, A8).
--
-- Per-user data: PURGED on erasure — a preference tied to a member id has
-- no Art 17(3) ground to survive one. Row in docs/data_register.md lands in
-- the same commit; user_id is already a conventional name in
-- privacy_service.SUBJECT_ID_COLUMNS, so the access export sees it for free.

CREATE TABLE IF NOT EXISTS mahjong_prefs (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    mode       TEXT    NOT NULL,
    updated_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
