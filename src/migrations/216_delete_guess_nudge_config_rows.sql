-- Migration 216: delete the Guess inactivity nudge's config rows
-- (2026-09-07, removal of the nudge loop, service and dashboard dial).
-- Migrations 191 and 195 did the same for keys their features left behind; a
-- stale key is a key someone later mistakes for a setting, so they go rather
-- than being blanked. Both were read back read-only from the live database
-- first: the home guild carries `guess_inactivity_ping_hours` = 4 and
-- `guess_last_nudged_round_id` = 432, so this deletes real rows, not nothing.
--
-- 1. `guess_inactivity_ping_hours`. The dial behind a 15-minute loop that
--    pinged the Guess role about an open round gone quiet. Guess Who is a
--    game people find on their own; a recurring role ping chasing them back
--    to it was noise. The loop, its service and the dashboard field are gone
--    with this migration, so the key has no reader left.
--
-- 2. `guess_last_nudged_round_id`. Not a setting at all — the loop's own
--    memory of which round it last pinged about, kept in `config` for want of
--    anywhere better. Nothing writes or reads it now.
--
-- Both sit in `settings_registry.DEAD_KEYS` so the Config Advisor cannot
-- offer the dial back for a reader that no longer exists.

DELETE FROM config
 WHERE key IN ('guess_inactivity_ping_hours', 'guess_last_nudged_round_id');
