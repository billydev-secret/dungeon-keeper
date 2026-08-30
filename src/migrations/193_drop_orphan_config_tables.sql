-- Migration 193: drop three tables nothing in src/ reads or writes
-- (2026-08-30 dashboard configuration IA audit, findings 11/40/53). Billy gave
-- the explicit go-ahead for these — every one holds live prod rows, which is
-- why the audit left them unwritten. 192 is taken by feature_rotation on
-- another branch.
--
-- 1. `give_role_permissions` (finding 40). The pre-`grant_role_permissions`
--    shape of the "Who Can Hand This Out" allow-list. No reader or writer
--    anywhere in src/ for as long as git remembers, and no migration creates
--    it — it is a relic of application code that was deleted. Prod holds 2
--    rows granting two members the right to hand out one role; both are a
--    silent no-op, invisible to the Role Grants panel, and the surviving
--    `grant_role_permissions` table is where the live allow-list lives. The
--    rows name members in `entity_id`, so this is also personal data that no
--    surface could show or erase in a way an admin would recognise: dropping
--    it is the fix, not a loss. `docs/data_register.md` records the outcome.
--
-- 2. `dm_request_channels` (finding 11). The DM Permissions panel's "Request
--    Channel" picker wrote here, but DM requests have always been delivered
--    by DMing the target with consent buttons — nothing was ever posted to
--    the configured channel, and moderators were never the approvers. The
--    picker and its false hint went with the defect queue; this drops the
--    four stored channel ids (one of them already 0) that outlived it. Only
--    `000_init.sql` creates the table, so a fresh database builds it and this
--    migration removes it again — harmless, and cheaper than editing history.
--
-- 3. `music_channel_settings` (finding 53). Created by migration 006 for the
--    voice 24/7 / autoplay feature, which was deleted 2026-07-28 along with
--    every reader (`music_spec.md` §Storage records it as an orphan awaiting
--    exactly this). One live prod row survives — guild 1476…484's always-on
--    voice channel and its Spotify autoplay playlist — a stored preference no
--    code consults and no dashboard surface can see or clear. It also carries
--    `updated_by_user_id`, so like `give_role_permissions` it is an
--    unregistered personal-data store; dropping it closes that too. Reviving
--    24/7 playback is a new build, not a re-enable of this row.
--
-- No code path names any of the three, so nothing starts raising when they go.
-- Not reversible: the rows are gone. All three were verified against the live
-- database read-only before this was written.

DROP TABLE IF EXISTS give_role_permissions;

DROP TABLE IF EXISTS dm_request_channels;

DROP TABLE IF EXISTS music_channel_settings;
