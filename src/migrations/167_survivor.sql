-- 167_survivor.sql
-- (renumbered THRICE pre-ship: main took 164 (casino mines), 165 (music
-- playlists), then 166 (todo chore board) while this branch was built)
-- Survivor: NFL pick'em survival pool (docs/survivor_spec.md v2.2, amended
-- 2026-08-17). Five tables, laid down together in stage 1 of
-- docs/plans/survivor.md so no later stage needs a schema change:
--
--   survivor_seasons  one row per season; every rule is a key in `config`
--                     (JSON, see spec §5), merged over service defaults so a
--                     season created today keeps its rules if defaults move.
--   survivor_players  one entry per person per season (spec §1.1). status is
--                     the whole life state: alive or ghost — the dead keep
--                     playing (Ghost Streak, §1.7), so nobody is deleted.
--   survivor_picks    the satchel. slot is 1 except in double-pick weeks
--                     (§1.5), present from day one so week 14 needs no
--                     migration. result stays NULL until the game settles.
--   nfl_games         the ingested schedule + results. Keyed by season_year,
--                     not guild — league facts are global, shared by every
--                     guild's season. favorite/favorite_prob are frozen at the
--                     last pre-kickoff poll; the gauntlet replay (§4.2) reads
--                     ONLY these stored values, never live odds, which is what
--                     makes two same-week joiners inherit identical lines.
--   survivor_flavor   the Reckoning's voice (§2.5): eulogy/toll/no_death/annul
--                     lines with {name}/{team}/{week} slots. Guild-scoped —
--                     two live guilds — and seeded per guild at first
--                     create-season.
--
-- Per-user data: survivor_players and survivor_picks. Both are PURGED on
-- erasure (privacy_service.purge_user_data; decision recorded in
-- docs/data_register.md — picks are a game record with no Art 17(3) ground to
-- outlive the member). `user_id` is already in SUBJECT_ID_COLUMNS, so the
-- access-request export sees both without changes.

CREATE TABLE IF NOT EXISTS survivor_seasons (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    season_year INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'enrolling',   -- enrolling|active|complete
    config TEXT NOT NULL DEFAULT '{}'           -- JSON, spec §5
);

CREATE INDEX IF NOT EXISTS idx_survivor_seasons_guild
    ON survivor_seasons (guild_id, status);

-- One live season per guild (spec §6.12) enforced at the schema, not just the
-- service's check-then-insert — two concurrent creates (double-click, second
-- admin) would otherwise both pass the check and leave a hidden second season.
CREATE UNIQUE INDEX IF NOT EXISTS idx_survivor_one_live_season
    ON survivor_seasons (guild_id) WHERE status != 'complete';

CREATE TABLE IF NOT EXISTS survivor_players (
    season_id INTEGER NOT NULL REFERENCES survivor_seasons(id),
    -- Denormalized from the season (stage-4 review): the access export
    -- guild-scopes via a literal guild_id column, and without it a
    -- single-guild export would disclose the member's rows from every guild.
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'alive',       -- alive|ghost
    strikes_used INTEGER NOT NULL DEFAULT 0,
    eliminated_week INTEGER,
    -- Why they died (added with stage 4's settle engine, pre-ship):
    --   'picks'  derived from graded results — the settle engine recomputes
    --            it, so a corrected result can resurrect the player
    --   'cap'    the groundskeeper stopped covering (§1.2 auto-assign cap)
    --   'missed' missed_pick='eliminate' seasons: pickless at final kickoff
    --   'admin'  dashboard eliminate
    --   'left'   walked out of the meadow (§6.14)
    --   'entry'  late_entry='ghost_only' arrival — born a ghost
    -- Non-'picks' deaths survive recomputation; NULL while alive.
    elimination_source TEXT,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (season_id, user_id)
);

-- The erasure path and the export both come in by user, not season.
CREATE INDEX IF NOT EXISTS idx_survivor_players_user
    ON survivor_players (user_id);

CREATE TABLE IF NOT EXISTS survivor_picks (
    season_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,              -- denormalized; see survivor_players
    user_id INTEGER NOT NULL,
    week INTEGER NOT NULL,
    slot INTEGER NOT NULL DEFAULT 1,            -- 2 exists only in double-pick weeks
    team TEXT NOT NULL,                         -- ESPN abbr, e.g. 'SF'
    game_id TEXT NOT NULL,
    auto_assigned INTEGER NOT NULL DEFAULT 0,   -- 📎 the groundskeeper
    locked_at TEXT,
    result TEXT,                                -- NULL|win|loss|tie|void
    PRIMARY KEY (season_id, user_id, week, slot)
);

CREATE INDEX IF NOT EXISTS idx_survivor_picks_week
    ON survivor_picks (season_id, week);
CREATE INDEX IF NOT EXISTS idx_survivor_picks_user
    ON survivor_picks (user_id);

CREATE TABLE IF NOT EXISTS nfl_games (
    season_year INTEGER NOT NULL,
    week INTEGER NOT NULL,
    game_id TEXT NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    kickoff_utc TEXT NOT NULL,                  -- ISO 8601 UTC; locks validate
                                                -- against the CURRENT value
                                                -- (flex scheduling, §6.2)
    status TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled|in|final|postponed
    favorite TEXT,                              -- abbr, frozen at last poll
                                                -- before kickoff
    favorite_prob REAL,                         -- its win probability; split
                                                -- from `favorite` because the
                                                -- gauntlet and auto-assign
                                                -- rank by the number
    winner TEXT,                                -- abbr | 'TIE' | NULL
    result_source TEXT,                         -- NULL = feed; 'manual' = settled by
                                                -- hand on the panel. The feed never
                                                -- overwrites a manual result — a poll
                                                -- flipping a VOID back was the
                                                -- stage-4 review's headline
    PRIMARY KEY (season_year, game_id)
);

CREATE INDEX IF NOT EXISTS idx_nfl_games_week
    ON nfl_games (season_year, week);

CREATE TABLE IF NOT EXISTS survivor_flavor (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    category TEXT NOT NULL,                     -- eulogy|toll|no_death|annul
    line TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_survivor_flavor_guild
    ON survivor_flavor (guild_id, category);
