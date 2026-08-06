-- Migration 151: seed `anon_audit_log` with the confessions still held in
-- `confession_threads`, now that the Confessions audit panel reads the audit
-- table instead of the operational one.
--
-- WHY: the mod-log Discord channel became optional (it de-anonymised every
-- confession to anyone who could read the channel), which promoted the web
-- panel to the moderation view. That panel used to read `confession_threads`
-- — a table purged hourly at a seven-day TTL because it carries thread
-- identity and reply routing, not because seven days is a sensible audit
-- window. Reading `anon_audit_log` instead decouples the two lifetimes: the
-- operational rows still expire in a week, the moderator trail lives for the
-- guild's anon-audit retention window (default 90 days).
--
-- Migration 145 deliberately did NOT route confessions onto this table, on the
-- grounds that `confession_threads` is load-bearing and a retention purge over
-- it would break thread identity. That reasoning is untouched and still holds:
-- nothing is migrated *off* `confession_threads`, which keeps its own TTL and
-- remains the source of truth for replies. Confessions merely also *write*
-- here now. Whisper and Guess stay out for 145's original reason.
--
-- Without this backfill the panel would read empty on the day it ships, even
-- though up to a week of confessions is sitting in the operational table.
-- Everything older than seven days is already gone and is not recoverable —
-- that history only ever existed in the Discord log channel.
--
-- created_at carries across unchanged (INTEGER unix seconds into a REAL
-- column), so backfilled rows expire on the same retention clock as new ones
-- and a guild past its window sees them purged on the next sweep.
--
-- root_message_id goes into `extra` as a JSON *string*: the dashboard reads it,
-- and a bare snowflake past 2^53 loses precision in JavaScript.
--
-- Runs once, so the WHERE NOT EXISTS guard is belt-and-braces against a
-- re-application against a DB that already has rows.

INSERT INTO anon_audit_log (
    guild_id, feature, event, actor_id, target_id,
    game_id, message_id, channel_id, extra, created_at
)
SELECT
    ct.guild_id,
    'confessions',
    -- A root confession is the row whose message is its own thread root;
    -- anything else is an anonymous reply within someone's thread.
    CASE WHEN ct.root_message_id = ct.message_id
         THEN 'confession_posted'
         ELSE 'reply_posted' END,
    ct.original_author_id,
    -- target_id is the member being replied to. Recoverable only for replies,
    -- by looking up the author of the root row in the same guild; NULL when
    -- the root has already aged out, which is not the same as "nobody".
    CASE WHEN ct.root_message_id = ct.message_id THEN NULL ELSE (
        SELECT root.original_author_id
        FROM confession_threads root
        WHERE root.guild_id = ct.guild_id
          AND root.message_id = ct.root_message_id
    ) END,
    NULL,
    ct.message_id,
    ct.channel_id,
    json_object('root_message_id', CAST(ct.root_message_id AS TEXT),
                'backfilled', 1),
    ct.created_at
FROM confession_threads ct
WHERE NOT EXISTS (
    SELECT 1 FROM anon_audit_log a
    WHERE a.feature = 'confessions'
      AND a.guild_id = ct.guild_id
      AND a.message_id = ct.message_id
);
