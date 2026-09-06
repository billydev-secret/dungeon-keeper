-- Migration 213: the Risky Rolls fallback backs off and gives up
-- (2026-09-05, ship review of the games deep review, package P6).
--
-- Migration 210 gave the payoff a fallback: after N hours with no question
-- from the winner, the bot draws a Truth from the bank and posts it for
-- them. When that post fails it returns False and leaves the prompt alone
-- "for the next tick" -- but the next tick is five minutes away and the
-- failure modes are the slow kind: an empty question bank, a channel that
-- has been deleted, a pairing the no-contact list now forbids. A prompt in
-- that state was retried every five minutes for the seven days until the
-- sweep aged it out, roughly two thousand attempts and two thousand log
-- lines per stuck prompt.
--
-- `fallback_attempts` counts the failures and `fallback_attempted_at` says
-- when the last one was, so the chaser waits an hour, then two, and after
-- three failures abandons the prompt instead of picking it up again.
--
-- Per-user data: neither column names a member; both are counters/timestamps
-- on a table already registered and purged whole (docs/data_register.md).

ALTER TABLE risky_pending_questions ADD COLUMN fallback_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE risky_pending_questions ADD COLUMN fallback_attempted_at REAL;
