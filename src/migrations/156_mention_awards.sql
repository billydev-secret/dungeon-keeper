-- Migration 156: Mention Awards — pay a member who gets @-mentioned alongside
-- a configured trigger phrase.
--
-- WHY: several games in the servers are run by members, not by the bot. The
-- one that prompted this is Hot Seat, where the outgoing contestant announces
-- their successor with a card image and a ping ("@Hot Seat your turn
-- @turbodog8!"). The bot hosts none of it, so no game hook can pay the
-- contestant — but the announcement itself is a clean, machine-readable event
-- the bot already sees.
--
-- Rather than hardcode Hot Seat, a rule is (channel, phrase, amount,
-- announcer role): when a message in `channel_id` from someone holding
-- `announcer_role_id` contains `phrase` and @-mentions exactly one member,
-- that member is paid `amount`. A second member-run game needs a second row,
-- not a second feature.
--
-- No per-member data lives here: a rule is guild configuration. `created_by`
-- is the admin who wrote it, kept for the same reason
-- `games_external_watch.set_by` is — an audit trail on who opened a faucet.
--
-- Payout idempotency deliberately reuses `games_external_payouts`
-- (message_id PK, `kind` discriminator) rather than adding a second ledger.
-- Hot Seat is an external game in the sense that matters: the bot doesn't
-- host it, and each announcement may pay exactly once.
--
-- `enabled` is absent by design — the four levers are the whole surface, and
-- clearing `channel_id` is how a rule is parked. Deleting the row is how it
-- is removed.

CREATE TABLE IF NOT EXISTS mention_award_rules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    -- Matched case-insensitively as a substring of the message content.
    -- Content is never stored — the listener reads it live off the gateway.
    phrase            TEXT    NOT NULL,
    amount            INTEGER NOT NULL DEFAULT 0,
    -- 0 = anyone in the channel may award. Set it to require a role, which is
    -- the anti-farm lever: without it, any member can pay any other member by
    -- typing the phrase.
    announcer_role_id INTEGER NOT NULL DEFAULT 0,
    created_by        INTEGER,
    created_at        REAL    NOT NULL DEFAULT (strftime('%s','now'))
);

-- The listener's hot path: every message in a watched guild looks up its
-- channel's rules.
CREATE INDEX IF NOT EXISTS idx_mention_award_rules_channel
    ON mention_award_rules (guild_id, channel_id);
