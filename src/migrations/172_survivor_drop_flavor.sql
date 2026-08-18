-- Drop the Survivor flavor corpus (Billy, 2026-08-18 first-look review:
-- "just the facts"). The feature is removed whole — service CRUD, dashboard
-- card, Reckoning rotation — and the Reckoning's copy is hardcoded factual,
-- so the per-guild voice route this table carried is gone with it.
--
-- No per-user data: rows were guild-scoped (guild_id, category, line,
-- active) template lines, seeded from a shipped default at season creation.
-- Nothing references the table after this migration's release.

DROP TABLE IF EXISTS survivor_flavor;
