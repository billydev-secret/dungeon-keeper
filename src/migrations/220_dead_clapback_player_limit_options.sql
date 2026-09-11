-- Migration 220: clear the retired player-limit dials out of games_game_config
-- (2026-09-11, finding #49 of the 2026-08-29 dashboard config audit).
--
-- Clapback's panel offered Minimum/Maximum Players until 2026-08-27, when the
-- dials were retired: the cog caps the lobby with its own MIN_PLAYERS /
-- MAX_PLAYERS constants and has never read the stored pair. Removing the dials
-- did not remove the row they had already written, and until 37e3f351 it could
-- not be removed from the dashboard either — the config PUT merged into the
-- stored options instead of replacing them, so every later save carried the
-- dead pair forward. That merge is fixed; this deletes what it stranded.
--
-- Read back read-only from the live database first. games_game_config holds
-- exactly two rows, and one of them carries the keys:
--
--   guild 1469...666 | clapback | {"min_players": 2, "max_players": 16}
--   guild 1469...666 | photo    | {"channel_id": "1528...088", ...}
--
-- so this touches one row, which becomes {}. No behaviour changes: nothing has
-- read those two keys for clapback since the dials were retired, and an absent
-- key and a stored 2/16 both resolve to the cog's constants.
--
-- The hazard here is the one migration 217 recorded in a different shape:
-- min_players and max_players are NOT dead everywhere. Most Likely To and
-- Mt. Rushmore Draft have a join phase, still offer both dials, and still read
-- them (games_mlt_cog.py:519, games_rushmore_cog.py:878) — a blanket
-- json_remove over the table would silently delete two live settings for two
-- games. So the game types are enumerated, never matched by key name, and
-- tests/web/test_game_dials_are_enforced.py::test_only_the_games_with_a_lobby_offer_player_limits
-- is what keeps that list of two honest.
--
-- The other four names are the rest of RETIRED's holders of this pair (wyr,
-- ama, nhie, price — their dials went the same way). Prod carries no row for
-- any of them, so they are a guard against a second guild's leftovers rather
-- than a deletion anyone will observe.

UPDATE games_game_config
   SET options = json_remove(options, '$.min_players', '$.max_players'),
       updated_at = CURRENT_TIMESTAMP
 WHERE game_type IN ('clapback', 'wyr', 'ama', 'nhie', 'price')
   AND json_valid(options)
   AND (json_extract(options, '$.min_players') IS NOT NULL
        OR json_extract(options, '$.max_players') IS NOT NULL);
