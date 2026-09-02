-- 201_newcomer_funnel_indexes.sql
-- Newcomer Funnel (compute_newcomer_funnel) runs four queries per recent
-- joiner in a Python loop -- there is no way to fold "per-user milestone
-- since their own join time" into one set query, so the fix is making each
-- of those four queries cheap, not eliminating the loop.
--
-- Before this migration the only relevant indexes were (guild_id, ts) and
-- (guild_id, author_id) -- author_id without ts, so every "this user's
-- messages since they joined" lookup degraded to a guild-wide ts range scan
-- with author_id applied as a residual filter. The first-reply query is
-- worse: it also filters the *other* side of the join, m.reply_to_id IN
-- (subquery), and there was no index on reply_to_id at all, so SQLite had to
-- walk every message in the guild since the joiner's join_ts and bloom-test
-- each one's reply_to_id against the subquery's result set.
--
-- Measured on a scratch copy of the prod table (785,478 rows total, 602,710
-- in the guild used for the benchmark) replaying the exact four per-user
-- queries for that guild's 197 members who joined in the last 90 days:
--   whole newcomer-funnel loop     37.4s  -> 0.18s   (~210x)
--   first-message MIN(ts)          0.88s  -> 0.002s
--   3+-channels COUNT(DISTINCT)   18.1s   -> 0.06s
--   first-reply correlated query  25.4s   -> negligible (folds into the 0.18s above)
-- Plans changed from "SEARCH messages USING INDEX idx_messages_guild_ts
-- (guild_id=? AND ts>?)" (a guild-wide range with author_id/reply_to_id as a
-- residual filter) to a direct seek on the new composite indexes for all
-- four queries; for the reply lookup specifically, SQLite flips which side
-- of the IN (subquery) it drives from once reply_to_id is indexed, turning a
-- guild-wide scan into one seek per candidate message.
--
-- idx_messages_author_ts covers the first-message, 3+-channels and D7-return
-- queries (all "this author's messages since ts"). idx_messages_reply_to
-- covers the first-reply query's "who replied to one of this author's
-- messages" half. Neither is redundant with the existing (guild_id, ts) or
-- (guild_id, author_id) indexes -- both lack the column the newcomer-funnel
-- access pattern seeks on.

CREATE INDEX IF NOT EXISTS idx_messages_author_ts
    ON messages (guild_id, author_id, ts);

CREATE INDEX IF NOT EXISTS idx_messages_reply_to
    ON messages (guild_id, reply_to_id);
