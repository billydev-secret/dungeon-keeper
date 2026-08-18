-- 169_music_playlist_retry.sql
-- Auto-retry for the processed-message ledger. Rows in a retryable status
-- (write_failed / resolve_failed) existed from day one, but nothing re-fired
-- them except a manual Re-scan. A timer sweep now retries them with
-- exponential backoff; `attempts` is its counter and backoff exponent
-- (due when processed_at + base * 2^attempts has passed, capped at a
-- max attempt count — policy constants live in music_playlist_service).
-- Not per-user data: the ledger names messages, not members.
ALTER TABLE music_playlist_messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
