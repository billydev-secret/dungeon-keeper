-- 163_known_channels_thread_parent.sql
-- Channel metrics: tell a thread apart from a channel, and remember its parent.
--
-- The channels analytics panel groups `messages` by channel_id and calls each
-- group a channel. For a message posted in a thread, channel_id *is* the
-- thread's own id, so every thread became a row in a view meant to show real
-- channel health — 243 of the 367 rows in a 30-day window were not channels at
-- all. Threads are throwaway by design; their activity belongs to the channel
-- they were started from.
--
-- Nothing in the schema could distinguish the two after the fact. `messages`
-- records only channel_id, and known_channels held threads and channels in the
-- same shape. These two columns are that missing fact, kept on the channel
-- registry rather than on `messages` because it is a property of the channel,
-- not of each message — 2.5k rows to maintain instead of millions to rewrite,
-- and it survives the thread being deleted on Discord.
--
--   is_thread  1 for a thread, 0 for a channel. Written at ingest from
--              discord.py's Thread.parent_id, which exists on Thread and on no
--              other channel type — so it can never mistake a text channel's
--              *category* for a parent.
--   parent_id  the channel the thread hangs off (NULL for a real channel, and
--              for a thread whose parent we never learned).
--
-- Backfill, part 1 of 2 — free and offline. A thread created from a message
-- takes that message's id as its own, so any thread whose starter message we
-- archived can be recovered with a self-join and no Discord call. That covers
-- roughly a fifth of the historical tail; scripts/backfill_thread_parents.py
-- walks the live guild's active and archived threads for the rest.
--
-- Rows this can't resolve stay is_thread=0/parent_id=NULL, i.e. indistinguish-
-- able from a channel — but the resolver in services/channel_rollup.py drops
-- any id the live guild doesn't list as a current channel, so an unrecovered
-- thread falls out of the metrics rather than lingering as a bogus row.

ALTER TABLE known_channels ADD COLUMN parent_id INTEGER;
ALTER TABLE known_channels ADD COLUMN is_thread INTEGER NOT NULL DEFAULT 0;

-- Threads whose starter message is in the archive. The guild_id match keeps a
-- cross-guild id collision (impossible for real snowflakes, possible in a test
-- fixture) from inventing a parent, and channel_id <> the starter's channel_id
-- skips the self-referential case: a forum post's id equals its own first
-- message, which lives *inside* the thread and so names no parent.
UPDATE known_channels
   SET parent_id = (
           SELECT m.channel_id FROM messages m
            WHERE m.message_id = known_channels.channel_id
              AND m.guild_id   = known_channels.guild_id
              AND m.channel_id <> known_channels.channel_id
       ),
       is_thread = 1
 WHERE EXISTS (
           SELECT 1 FROM messages m
            WHERE m.message_id = known_channels.channel_id
              AND m.guild_id   = known_channels.guild_id
              AND m.channel_id <> known_channels.channel_id
       );

-- The self-referential hits are threads too — we just don't know the parent.
-- Marking them still helps: is_thread=1 alone is enough for the resolver to
-- drop them when the live guild is unreachable and it can't check ids.
UPDATE known_channels
   SET is_thread = 1
 WHERE is_thread = 0
   AND EXISTS (
           SELECT 1 FROM messages m
            WHERE m.message_id = known_channels.channel_id
              AND m.guild_id   = known_channels.guild_id
       );

-- The resolver looks up parents by (guild_id, channel_id), which the primary
-- key already serves. No new index: this table is small and read whole.
