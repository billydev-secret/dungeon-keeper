-- Migration 211: the anonymous-alias map gets a clock (2026-09-04, anon-tail-69).
--
-- `confession_emoji_assignments` is the user -> pseudonym map behind every
-- persistent anonymous reply: a confession thread's, and — since FFA reused
-- the confession machinery — an Anonymous Truth or Dare prompt's. It had no
-- timestamp, so the seven-day confessions sweep (which already drops
-- `confession_threads`) could never reach it and 445 thread-less rows had
-- piled up in prod, each still naming a member beside an alias.
--
-- PER-USER DATA. Rows now carry `created_at` and share the threads' seven-day
-- TTL (`confessions_service.purge_old_thread_posts`), FFA clears its own
-- prompts' rows when the host closes the game, and `purge_user_data` deletes
-- a member's rows outright. Existing rows default to 0 and go on the next
-- sweep: a thread older than a week has already lost its routing row, so
-- nothing that can still be replied to loses its alias. See
-- docs/data_register.md.

ALTER TABLE confession_emoji_assignments
    ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0;
