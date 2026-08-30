-- Migration 195: delete config rows that no code reads any more
-- (2026-08-30 dashboard configuration IA audit, findings 10/19/41/43/51/57).
-- Migration 191 did this for three economy keys after the channel-field audit;
-- this is the same operation for the keys the wider config-surface audit found.
-- A stale key is a key someone later mistakes for a setting, so they are
-- deleted rather than blanked.
--
-- 1. `ticket_panel_channel_id` / `ticket_panel_message_id` (41). The live
--    panel record moved to the `ticket_panels` table in migration 023. Prod
--    carries both at guild 0 and at the home guild; nothing reads either.
--    They stay in `settings_registry.DEAD_KEYS` so the advisor cannot
--    resurface them.
--
-- 2. `ai_mod_model`, `ai_wellness_model` and any `ai_model_*` (10). The eight
--    model dropdowns on the AI panel wrote these, but `ollama_client.chat()`
--    ignores its model argument on both backends — one model is loaded at a
--    time, chosen by the model-source fields. The dropdowns went with the
--    defect queue; these are the four values they left behind. The `LIKE`
--    matches nothing in prod today and is there for the per-command keys the
--    panel could have written.
--
-- 3. `econ_price_text_room`, `econ_price_voice_room` (43). Priced for the
--    stage-6 private-rooms feature, which was never built: no purchase path
--    reads them, and the affordability card that displayed them is gone. Four
--    rows across both live guilds, one hand-tuned to 230 — which is exactly
--    the trap, since it reads as a setting someone deliberately chose.
--
-- 4. `econ_quest_board_monthly` (57). Monthly became a single guild-wide goal
--    rather than a personal-board draw, so the board draw excludes it and no
--    panel exposes it; its last attribute read was already unreachable.
--
-- 5. The legacy guild-0 grant block (51): `denizen`/`nsfw`/`veteran` role id,
--    grant message, announce channel and log channel — 12 rows, superseded by
--    the `grant_roles` table. Their only remaining reader is the one-shot
--    `db_utils.migrate_grant_roles`, which early-returns for any guild that
--    already has `grant_roles` rows. That is the home guild only: three other
--    guilds have none, so today that one-shot would seed each of them from
--    *the home guild's* role ids and welcome copy, read through the guild-0
--    legacy fallback. Deleting these rows means it seeds an empty template
--    instead, which is the correct outcome and the reason this is a fix
--    rather than only a tidy-up. Keys are enumerated and scoped to guild 0 on
--    purpose: a `LIKE 'nsfw_%'` would also match the live Image Guard keys
--    (`nsfw_classifier_threshold`, `nsfw_sfw_prevention_mode`, …) on the home
--    guild.
--
-- 6. `greeting_watch_notify_user_id` (19). The pre-multi-subscriber single
--    key. Both the DM loop and the panel fall back to it whenever the CSV is
--    empty, so it could silently resurrect a subscriber an admin had just
--    removed. The PUT now blanks it on every save; this clears the one row
--    that predates that fix. It holds the same member id the CSV already
--    lists, so no subscriber changes today.
--
-- Config rows only — no schema change, and nothing to roll back beyond
-- re-inserting values no code path consults. Every row was read back from the
-- live database read-only before this was written.

DELETE FROM config WHERE key IN (
    'ticket_panel_channel_id',
    'ticket_panel_message_id',
    'ai_mod_model',
    'ai_wellness_model',
    'econ_price_text_room',
    'econ_price_voice_room',
    'econ_quest_board_monthly',
    'greeting_watch_notify_user_id'
);

DELETE FROM config WHERE key LIKE 'ai_model_%';

DELETE FROM config WHERE guild_id = 0 AND key IN (
    'denizen_role_id', 'denizen_grant_message',
    'denizen_announce_channel_id', 'denizen_log_channel_id',
    'nsfw_role_id', 'nsfw_grant_message',
    'nsfw_announce_channel_id', 'nsfw_log_channel_id',
    'veteran_role_id', 'veteran_grant_message',
    'veteran_announce_channel_id', 'veteran_log_channel_id'
);
