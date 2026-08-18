-- 170_drop_expand_albums_dial.sql
-- The expand_albums dial is retired: an album or playlist link now always
-- contributes exactly its single most-popular track (decided 2026-08-17 in
-- docs/plans/music-playlist-cog.md, built with the 2026-08-18 defect fixes).
-- A config key whose reader is gone would still read as "configured" on the
-- config table while doing nothing, so the stored rows go with it.
DELETE FROM config WHERE key = 'music_playlist_expand_albums';
