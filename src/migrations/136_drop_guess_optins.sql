-- Migration 136: drop the dead guess_optins table.
--
-- The table (veil_optins, renamed in migration 020) has never been read or
-- written by the cog or the web server — its whole CRUD surface in
-- guess_repo.py had no callers outside its own unit tests. Guess eligibility
-- is derived live from Discord role membership (guess_role.members), never
-- from this table, so there is nothing to migrate out of it. Verified empty in
-- production (0 rows against 327 rounds) before dropping. No foreign keys or
-- non-autoindex indexes referenced it.

DROP TABLE IF EXISTS guess_optins;
