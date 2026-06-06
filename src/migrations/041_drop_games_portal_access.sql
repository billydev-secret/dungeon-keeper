-- 041_drop_games_portal_access.sql
-- Removes the orphaned games_portal_access table. The /games portal-grant
-- commands that wrote it were deleted; portal/Games access is now governed
-- solely by Discord admin/mod permissions and the games_editor_role (Game
-- Host Role) set from the dashboard, neither of which ever read this table.

DROP TABLE IF EXISTS games_portal_access;
