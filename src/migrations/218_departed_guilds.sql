-- Departed-guild markers: the queue for erasing a server's data after the bot
-- is removed from it.
--
-- Until now, being kicked from a server erased five config keys and nothing
-- else. Every message, ledger row, moderation record and game result stayed in
-- the database forever, keyed to a guild the bot can no longer see and no
-- admin can reach — there is no dashboard for a server the bot has left. This
-- table is what makes that data reachable again, by the sweep that deletes it.
--
-- One row per guild the bot is no longer in. ``purge_after`` is written once,
-- at the moment of departure, from the operator's configured grace period; the
-- sweep only ever compares it to the clock. It is deliberately not recomputed
-- from config at sweep time, because the guild's own config rows are among the
-- things the purge deletes, and a deadline that re-derives itself would shift
-- under every guild in the queue each time the dial moved.
--
-- ``channel_ids`` is a comma-separated snapshot of the guild's channels, taken
-- at departure. Two tables (the LegitLibs per-channel tier and the games
-- session tracker) key on a channel id and nothing in the database maps a
-- channel back to a guild, so they are reachable only from Discord's own
-- channel list — which exists for exactly as long as the removal handler holds
-- the Guild object. Storing it here is what lets the purge run days later and
-- still find those rows. A row marked by reconciliation has no snapshot to
-- take and leaves this empty.
--
-- ``source`` distinguishes 'event' (an on_guild_remove that the bot saw
-- happen) from 'reconcile' (a guild that vanished while the bot was offline,
-- noticed later by comparing the database against the connected guild list).
-- Reconciliation only ever *marks*; it never purges on the spot, so a Discord
-- outage that returns a short guild list self-heals when the guild reappears
-- and the row is deleted again.
--
-- This ships inert. The table is created empty, and nothing writes to it until
-- an operator sets the purge dial, which is unset by default and means "never
-- purge" while it stays that way.

CREATE TABLE IF NOT EXISTS departed_guilds (
    guild_id    INTEGER PRIMARY KEY,
    departed_at REAL    NOT NULL,
    purge_after REAL    NOT NULL,
    channel_ids TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT 'event'
);

CREATE INDEX IF NOT EXISTS idx_departed_guilds_due ON departed_guilds (purge_after);
