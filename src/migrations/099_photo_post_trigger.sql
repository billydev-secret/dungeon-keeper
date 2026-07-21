-- Photo Challenge payout switches from reaction-gating to paying on the post.
--
-- Old model: the `photo_react` trigger paid a member whose image post in the
-- configured photo channel drew N distinct human reactions (default 5).
-- New model: posting an image in that channel pays on its own — no reactions
-- needed — still capped once per guild-local day (occurrence
-- `photo_post:<local_day>`, like voice_session/boost). The reaction listener,
-- distinct-reactor count, and the react_threshold/auto_react config knobs are
-- retired in code.
--
-- Rename the trigger kind on existing quests and income-source rows so an
-- admin's live `photo_react` quest (main guild's "Picture This", id 17) and any
-- enable/disable toggle keeps working under the new name. UPDATE OR REPLACE
-- guards the income-source PK (guild_id, source) in the impossible-but-safe
-- case a photo_post row already exists. Idempotent: a second run matches nothing.

UPDATE econ_quests
   SET trigger_kind = 'photo_post'
 WHERE trigger_kind = 'photo_react';

UPDATE OR REPLACE econ_income_sources
   SET source = 'photo_post'
 WHERE source = 'photo_react';
