-- Bump attribution (plan: quest-variety stage 1).
--
-- bump_tracker_log previously recorded only (guild, site, when) — no idea
-- WHO bumped. The bump quest kind needs the member: /bump log knows its
-- invoker, and an auto-detected listing-bot response carries the invoker in
-- message.interaction_metadata. 0 = unknown (pre-migration rows, or a
-- detector message with no interaction metadata).

ALTER TABLE bump_tracker_log ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
