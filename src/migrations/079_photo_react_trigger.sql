-- Photo Challenge payout switches from reply-detection to reaction-gating.
--
-- Old model: the `photo_reply` trigger paid a member who replied to a Photo
-- Challenge card with an image (keyed once per card, no time gate).
-- New model: a member's image post in the configured photo channel pays when
-- it earns N distinct human reactions (default 5, author + bots excluded),
-- capped once per guild-local day (occurrence `photo_react:<local_day>`, like
-- voice_session/boost). The reply path and the econ_photo_cards registry are
-- retired in code; the table is left in place (unused) rather than dropped.
--
-- Rename the trigger kind on existing quests and income-source rows so an
-- admin's live `photo_reply` quest (and any enable/disable toggle) keeps
-- working under the new name. UPDATE OR REPLACE guards the income-source PK
-- (guild_id, source) in the impossible-but-safe case a photo_react row exists.
-- Idempotent: a second run matches nothing.

UPDATE econ_quests
   SET trigger_kind = 'photo_react'
 WHERE trigger_kind = 'photo_reply';

UPDATE OR REPLACE econ_income_sources
   SET source = 'photo_react'
 WHERE source = 'photo_reply';
