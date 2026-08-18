-- 171_music_playlist_best_effort.sql
-- Review policy change (Billy, 2026-08-18): a YouTube link always adds its
-- best-scoring candidate — the confidence gate and its match_threshold dial
-- are gone (key swept below, same reasoning as 170's expand_albums) — and
-- reviewer verdicts are remembered per link.
--
-- Data half: the pending review backlog is exactly what the new pipeline
-- answers on its own, so re-fire every message that has a pending queue row.
-- The messages flip to the retryable status the auto-retry sweep consumes
-- (processed_at backdated so they are due on its first tick); their pending
-- rows are deleted — on re-process, a remembered approval re-adds the
-- reviewer's exact track and anything else adds its best candidate.
-- Resolved rows (approved/rejected) are history AND the verdict store: kept.
DELETE FROM config WHERE key = 'music_playlist_match_threshold';

UPDATE music_playlist_messages
   SET status = 'resolve_failed', attempts = 0, processed_at = 1000000000
 WHERE status = 'processed'
   AND EXISTS (
       SELECT 1 FROM music_playlist_unmatched p
        WHERE p.guild_id = music_playlist_messages.guild_id
          AND p.message_id = music_playlist_messages.message_id
          AND p.status = 'pending');

DELETE FROM music_playlist_unmatched WHERE status = 'pending';
