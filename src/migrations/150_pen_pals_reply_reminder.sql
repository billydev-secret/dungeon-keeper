-- Pen Pals: nudge a member who hasn't replied to their partner.
--
-- reply_reminder_seconds is the one dial: how long a room can sit with the
-- last member message unanswered before the quiet partner gets pinged.
-- 0 (the default) disables reminders entirely, so existing guilds see no
-- behavior change until a moderator sets it on the dashboard.
--
-- reply_reminder_sent_at stamps when the current lull was nudged. It is
-- compared against the timestamp of the last member message rather than
-- being cleared on reply: once the quiet partner speaks, that message is
-- newer than the stamp and the next lull re-arms on its own.

ALTER TABLE pen_pals_config ADD COLUMN reply_reminder_seconds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pen_pals_sessions ADD COLUMN reply_reminder_sent_at REAL NOT NULL DEFAULT 0;
