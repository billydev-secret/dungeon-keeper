-- Migration 210: Risky Rolls chases its payoff (2026-09-04, games deep
-- review rotation-rooms-156, docs/reviews/2026-09-02-games-deep-review-findings.md).
--
-- The round's payoff is the winner's question, and in most rounds it never
-- came: at least 12 rounds in one week resolved with a winner who never
-- pressed Ask Question, four of six posted questions sat unanswered for over
-- a day, and nothing re-pinged anyone -- the 7-day sweep just deleted the
-- prompt. Two dashboard dials now chase it (both ship at 0 = off): one
-- re-ping of the winner after N hours, and one re-ping of the answerer once
-- a question is posted; and after N hours with no question the bot draws a
-- Truth from the question bank and posts it as the winner's question, so the
-- loser still answers.
--
-- `chased_at` records that the one re-ping went out, so a restart cannot
-- send it twice. `from_bank` marks a posted question the bot drew for a
-- winner who ran out of time, so the reply render says so instead of
-- putting the bank's words in the winner's mouth.
--
-- Per-user data: no new column names a member. Both tables are already
-- registered (docs/data_register.md, purged whole); the register's rows
-- note the two flags.

ALTER TABLE risky_pending_questions ADD COLUMN chased_at REAL;
ALTER TABLE risky_posted_questions ADD COLUMN chased_at REAL;
ALTER TABLE risky_posted_questions ADD COLUMN from_bank INTEGER NOT NULL DEFAULT 0;
