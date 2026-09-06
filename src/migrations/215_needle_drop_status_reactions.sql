-- Migration 215: remove Needle's thread-status reaction machine
-- (2026-09-06). Auto-reactions stay; the status state machine goes.
--
-- Needle could react on the starter message with one of three markers —
-- 🔵 open, ✅ archived, 🔒 locked — swapping them as the thread's state
-- changed, and optionally clearing the open marker on the first reply. That
-- made a reaction *load-bearing*: the bot wrote it, read it back, and removed
-- it, so an emoji on a post meant something the bot was asserting.
--
-- Billy's call is that auto-reactions should cue people and nothing more.
-- The per-channel `default_reactions` list stays exactly as it is — the bot
-- adds those and then forgets them, which is the behaviour that was wanted.
-- Everything that made a reaction a status is removed: the two columns here,
-- the three guild emoji keys, the `on_thread_update` listener, and the
-- in-thread reply handler that existed only to take the open marker off.
--
-- `archive_immediately` never archived anything despite its name — it gated
-- the marker removal only, which needle_spec.md had to carry a paragraph of
-- apology for. That paragraph goes with the column.
--
-- Discards nothing on the live server: `needle_channels` has **zero rows**
-- (verified read-only before this was written), so no channel loses a
-- setting, and the three emoji keys sit at their shipped defaults on the one
-- guild that has them. No index references either column, so DROP COLUMN is
-- legal.

ALTER TABLE needle_channels DROP COLUMN status_reactions;
ALTER TABLE needle_channels DROP COLUMN archive_immediately;

DELETE FROM config WHERE key IN (
    'needle_emoji_unanswered',
    'needle_emoji_archived',
    'needle_emoji_locked'
);
