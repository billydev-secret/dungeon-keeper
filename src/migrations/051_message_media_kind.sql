-- Per-message media classification kept as metadata even when raw content /
-- attachment URLs are not retained (storage level "none"), so media-based
-- metrics (e.g. NSFW posting by gender) keep working without storing content.
-- Values: 'media' (non-gif image/video), 'gif', 'other', or NULL (no media).
ALTER TABLE messages ADD COLUMN media_kind TEXT;

-- Backfill from any retained attachment URLs. Precedence is applied by running
-- the broadest bucket first and overriding with higher-precedence buckets:
--   other  ->  gif  ->  media   (media wins when a message has mixed kinds).
-- Discord CDN URLs carry a "?ex=..." query string, so match on the path before
-- the first '?'.

UPDATE messages SET media_kind = 'other'
 WHERE media_kind IS NULL
   AND message_id IN (SELECT message_id FROM message_attachments);

UPDATE messages SET media_kind = 'gif'
 WHERE message_id IN (
   SELECT ma.message_id FROM message_attachments ma
   WHERE (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.gif'
 );

UPDATE messages SET media_kind = 'media'
 WHERE message_id IN (
   SELECT ma.message_id FROM message_attachments ma
   WHERE (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.jpg'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.jpeg'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.png'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.webp'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.bmp'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.mp4'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.mov'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.webm'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.avi'
      OR (CASE WHEN INSTR(ma.url, '?') > 0
               THEN SUBSTR(LOWER(ma.url), 1, INSTR(ma.url, '?') - 1)
               ELSE LOWER(ma.url) END) LIKE '%.mkv'
 );

CREATE INDEX IF NOT EXISTS idx_messages_media_kind
    ON messages (guild_id, media_kind);
