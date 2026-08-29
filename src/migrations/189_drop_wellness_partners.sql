-- Migration 189: retire the wellness partners system + two orphaned columns
-- (2026-08-28 wellness readiness review, decision 1 — Billy approved the CUT
-- and this DROP explicitly). 188 was taken by flash_themes on main while this
-- branch was in flight; renumbered at review time.
--
-- 1. `wellness_partners`. The accountability-partner system shipped with the
--    feature but was never used once (0 rows, all-time) and an accepted
--    partnership had no downstream effect — nothing outside the dashboard
--    list ever read the table; the promised streak-sharing and nudges were
--    never built. The request path also predated the no-contact rule and
--    never consulted the list, so removing the surface closes that gap
--    rather than patching it. All code paths (service section, api routes,
--    DM accept/decline buttons, panel, nav entry, purge/export branches) go
--    in the same commit; the nav id `wellness-partners` is retired, never
--    reused. Zero data loss — the table was empty in every environment.
--
-- 2. `wellness_users.cooldown_until`. Write-only since the Cooldown
--    enforcement level was retired 2026-07-30 (4f93a4f4): opt-out nulled it,
--    nothing ever read it. Prod carries only NULLs.
--
-- 3. `wellness_config.crisis_resource_url`. Orphaned by the same honesty
--    pass — zero readers and writers, empty in both prod rows; the service's
--    own DDL comment has flagged it as vestigial since then.
--
-- No index references either column, so DROP COLUMN is legal (the two
-- wellness_partners indexes fall with their table).

DROP TABLE IF EXISTS wellness_partners;

ALTER TABLE wellness_users DROP COLUMN cooldown_until;

ALTER TABLE wellness_config DROP COLUMN crisis_resource_url;
