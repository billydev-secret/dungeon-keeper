-- 021_grant_required_role.sql
-- Adds an optional required_role_id to grant_roles.
-- When non-zero, the target member must already hold that Discord role
-- before they can receive the grant (mods bypass this check).
ALTER TABLE grant_roles ADD COLUMN required_role_id INTEGER NOT NULL DEFAULT 0;
