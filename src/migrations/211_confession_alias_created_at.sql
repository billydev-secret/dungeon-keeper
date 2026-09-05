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
-- a member's rows outright. See docs/data_register.md.
--
-- Existing rows are BACKFILLED, not left at 0: an alias whose thread is still
-- inside its week must keep serving — a member who replied on Monday must get
-- the same name and colour on Friday, and a live Anonymous Truth or Dare
-- prompt keeps its aliases across the restart. Each row takes its root
-- thread's own `created_at` (so it ages out with the thread it serves) and,
-- where no thread row exists — an FFA prompt, or a thread the sweep has
-- already taken — the moment of migration, giving it a full week from now.
-- The sweep additionally never touches a row still at 0, so an unstamped
-- row can never be purged by accident.

ALTER TABLE confession_emoji_assignments
    ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0;

UPDATE confession_emoji_assignments
   SET created_at = COALESCE(
        (SELECT t.created_at
           FROM confession_threads t
          WHERE t.guild_id = confession_emoji_assignments.guild_id
            AND t.message_id = confession_emoji_assignments.root_message_id
            AND t.created_at > 0),
        CAST(strftime('%s', 'now') AS INTEGER)
   )
 WHERE created_at = 0;
