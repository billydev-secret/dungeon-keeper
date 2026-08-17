-- Migration 165: Music playlist — a watched channel becomes a rolling
-- Spotify playlist (docs/plans/music-playlist-cog.md, stage 1).
--
-- Three tables, one per question the pipeline asks:
--
-- music_playlist_tracks — the window and its history. A row per song a
-- message put in the playlist; rolling off, message deletion, and admin
-- removal all *mark* the row (removed_at + removal_reason) rather than
-- deleting it, because "what did we listen to in July" is answerable only if
-- rolled-off rows survive. The dedupe scope is the WINDOW, not all time — a
-- song that rolled off months ago is postable again — which is exactly what
-- the partial unique index says: one LIVE row per (guild, platform, playlist,
-- track), history rows unconstrained. `platform` defaults to 'spotify' so a
-- second writer (YouTube is scoped) lands as rows, not a schema rewrite.
--
-- Duplicate posts are rows too, born dead: when someone posts a song already
-- live in the window, the service records a row with removed_at set and
-- removal_reason 'duplicate'. That reference is what makes deletion honest —
-- two people can post the same song, the playlist holds it once, and deleting
-- one post must not silently revoke the other's. The store resurrects the
-- surviving reference instead of removing the track.
--
-- music_playlist_unmatched — the review queue. A link the resolver couldn't
-- confidently match lands here with its best candidate and score for a
-- dashboard approve/reject; nothing reaches the playlist below the threshold.
--
-- music_playlist_messages — processed-message ledger, so a restart or a
-- manual re-scan is idempotent: one row per message the pipeline has seen,
-- whatever came of it.
--
-- Per-user data: `added_by` names the poster in both tracks and unmatched —
-- the column is deliberately `added_by` (never `posted_by`) because it is
-- already in privacy_service.SUBJECT_ID_COLUMNS, so the access export sees
-- both tables with no code change. Both tables get data_register.md rows;
-- purge clears them (music_playlist_store.purge_member_rows) — there is no
-- integrity or legal-claims ground to preserve "who posted a song", and the
-- Spotify playlist itself is keyed by track id, not by poster.

CREATE TABLE IF NOT EXISTS music_playlist_tracks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    platform       TEXT    NOT NULL DEFAULT 'spotify',
    playlist_id    TEXT    NOT NULL,
    track_id       TEXT    NOT NULL,
    title          TEXT    NOT NULL DEFAULT '',
    artist         TEXT    NOT NULL DEFAULT '',
    source_url     TEXT,
    channel_id     INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    added_by       INTEGER NOT NULL,
    added_at       REAL    NOT NULL DEFAULT (strftime('%s','now')),
    removed_at     REAL,
    -- 'rolled_off' / 'message_deleted' / 'duplicate' / 'admin'
    removal_reason TEXT
);

-- The dedupe rule itself: one live playlist entry per track. Partial, so
-- history rows (rolled off, deleted, duplicate references) never block a
-- re-add.
CREATE UNIQUE INDEX IF NOT EXISTS idx_music_playlist_tracks_live
    ON music_playlist_tracks (guild_id, platform, playlist_id, track_id)
    WHERE removed_at IS NULL;

-- Window reads and trims walk one playlist's rows in insertion order.
CREATE INDEX IF NOT EXISTS idx_music_playlist_tracks_playlist
    ON music_playlist_tracks (guild_id, platform, playlist_id, id);

-- The raw-delete handler's lookup: which rows did this message put here.
CREATE INDEX IF NOT EXISTS idx_music_playlist_tracks_message
    ON music_playlist_tracks (guild_id, message_id);

CREATE TABLE IF NOT EXISTS music_playlist_unmatched (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    channel_id         INTEGER NOT NULL,
    message_id         INTEGER NOT NULL,
    source_url         TEXT    NOT NULL,
    -- What the parser pulled off the link (e.g. a YouTube title + channel),
    -- shown beside the candidate so a reviewer can judge the match.
    extracted_title    TEXT,
    extracted_channel  TEXT,
    candidate_track_id TEXT,
    candidate_name     TEXT,
    candidate_artist   TEXT,
    confidence         REAL,
    reason             TEXT,
    added_by           INTEGER NOT NULL,
    -- 'pending' / 'approved' / 'rejected'
    status             TEXT    NOT NULL DEFAULT 'pending',
    reviewed_by        INTEGER,
    reviewed_at        REAL,
    created_at         REAL    NOT NULL DEFAULT (strftime('%s','now'))
);

-- The dashboard queue reads pending rows only; partial, because reviewed
-- rows outnumber pending ones for the life of the feature.
CREATE INDEX IF NOT EXISTS idx_music_playlist_unmatched_pending
    ON music_playlist_unmatched (guild_id, id)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS music_playlist_messages (
    guild_id     INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'processed',
    processed_at REAL    NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (guild_id, message_id)
);
