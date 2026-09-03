-- Migration 202: community ballots on policy proposals (2026-09-03).
--
-- Policy voting has always been the mod team's: `/policy vote` posts a tally
-- into the private proposal channel, every configured mod and admin is an
-- eligible voter, and adoption requires unanimity. A community ballot is a
-- second, parallel object with its own venue, its own electorate and its own
-- arithmetic. See docs/plans/policy-tickets-member-voting.md (and the
-- "Decisions — Billy, 2026-09-03" section at its foot, which supersedes the
-- rest of that plan).
--
-- WHY A SEPARATE TABLE AND NOT `policy_votes`. Two reasons, neither
-- cosmetic. First the arithmetic is different and incompatible: the mod vote
-- is unanimity over a known roster, a ballot is a simple majority of whoever
-- turned up with ties failing. Second `policy_votes` has no `guild_id` and has
-- to be scoped through a parent join in `purge_user_data` (privacy_service);
-- a new table denormalises it and needs no special case.
--
-- WHY THE COUNTS ARE FROZEN ON THE BALLOT ROW. `policy_ballot_votes` is
-- purgeable on erasure — it is an activity record with no Art 17(3) ground,
-- the same call `policy_votes` gets. That is only safe because a closed
-- ballot's result no longer depends on those rows: yes/no/abstain counts and
-- the outcome are written onto `policy_ballots` at close, so erasing one
-- member's vote can never move a decision that was already announced. Same
-- construction as `mahjong_results` (settled hand preserved, seats purged) and
-- `econ_ledger` (the money record outlives the actor). Erasing from an *open*
-- ballot does remove the vote from the live tally, which is correct: an
-- erasure is an out-of-band operator act and the member is no longer a
-- participant.
--
-- A BALLOT OUTLIVES THE MOD TICKET. Resolving a policy vote archives and
-- deletes the private proposal channel. A ballot lives in a thread in an
-- ordinary channel that nothing in this subsystem deletes, and its record is
-- these rows; both survive by construction. `policy_id` keeps the link, and
-- `policy_tickets` is a preserved table, so the link never dangles.
--
-- The ballot's own `policy_tickets` row carries status 'ballot' while it runs
-- (then 'closed'). Deliberately outside the ('open','voting') set
-- `get_policy_ticket_by_channel` matches on: a mod running `/policy vote`
-- inside a ballot thread must not be able to start the unanimity mod-vote on
-- a community ballot, whose finalizer would then delete the thread.

CREATE TABLE IF NOT EXISTS policy_ballots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    -- The `policy_tickets` row this ballot is recorded as.
    policy_id     INTEGER NOT NULL REFERENCES policy_tickets(id),
    -- The channel the ballot was launched in; the electorate is whoever can
    -- see it. There is no role dial: a veterans-only ballot is a ballot
    -- launched in a veterans-only channel.
    channel_id    INTEGER NOT NULL DEFAULT 0,
    thread_id     INTEGER NOT NULL DEFAULT 0,
    message_id    INTEGER NOT NULL DEFAULT 0,
    question      TEXT    NOT NULL DEFAULT '',
    opened_by     INTEGER NOT NULL,
    opened_at     REAL    NOT NULL,
    -- 0 means "no deadline": the guild has set its voting-deadline dial to 0,
    -- so this ballot closes only when a moderator presses Close.
    closes_at     REAL    NOT NULL DEFAULT 0,
    closed_at     REAL,
    closed_by     INTEGER,
    yes_count     INTEGER,
    no_count      INTEGER,
    abstain_count INTEGER,
    outcome       TEXT
);

-- The dashboard reads "this guild's ballots, newest first".
CREATE INDEX IF NOT EXISTS idx_policy_ballots_guild
    ON policy_ballots (guild_id, opened_at);
-- The 60-second sweep reads "still open, deadline passed".
CREATE INDEX IF NOT EXISTS idx_policy_ballots_open
    ON policy_ballots (closed_at, closes_at);

CREATE TABLE IF NOT EXISTS policy_ballot_votes (
    ballot_id INTEGER NOT NULL REFERENCES policy_ballots(id),
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    choice    TEXT    NOT NULL,
    cast_at   REAL    NOT NULL,
    -- One vote each, changeable until close. Double-pressing upserts; it does
    -- not stack. (Alt accounts are not solvable at this layer and nothing here
    -- pretends otherwise.)
    PRIMARY KEY (ballot_id, user_id)
);
