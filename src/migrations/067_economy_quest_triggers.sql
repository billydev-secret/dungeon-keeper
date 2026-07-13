-- Economy quests — trigger-word auto-verification (spec §4.4).
--
-- `trigger_words` holds comma/newline-separated phrases; when a member's
-- message contains one (whole-phrase, case-insensitive), the bot claims the
-- quest on their behalf — instant quests pay on the spot, sign-off quests
-- file the pending claim and post the bank-channel card. Empty = no trigger.
--
-- `trigger_channel_id` optionally scopes matching to one channel; NULL means
-- any channel in the guild counts.

ALTER TABLE econ_quests ADD COLUMN trigger_words TEXT NOT NULL DEFAULT '';
ALTER TABLE econ_quests ADD COLUMN trigger_channel_id INTEGER;
