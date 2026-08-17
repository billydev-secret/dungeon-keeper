# Music Playlist — a watched channel becomes a rolling Spotify playlist

**Reference spec.** Describes the feature as built (2026-08-17). The design
history, the port analysis of OpenMusicBot, and the platform scoping
(YouTube-as-destination, Apple Music) live in the plan:
[plans/music-playlist-cog.md](plans/music-playlist-cog.md). Where that plan
left questions open, this build resolved them:

1. Album/playlist links are **skipped**, behind a default-off `expand_albums`
   dial.
2. Link parsing covers **Spotify + YouTube only** (no Apple Music /
   SoundCloud parsing this round).
3. Rolled-off history rows are **kept** — no ageing-out yet.
4. Source-message reactions are **on**: ✅ added, 🔁 duplicate, ❓ queued.

**Activation:** the feature is inert until (a) the service restarts with this
code, and (b) the bot's Spotify account re-consents at `/spotify/authorize` —
the pre-existing grant is read-only and the widened
`SPOTIFY_SCOPES` (`routes/spotify_oauth.py:34`, now including
`playlist-modify-private playlist-modify-public`) only take effect on a fresh
consent. The dashboard's connection chip reports this state in words
(read-only → "needs re-consent") rather than surfacing a bare 403.

## Shape

One listener on a single dashboard-picked channel. Each message with music
links runs the pipeline: **parse → resolve → dedupe → write → trim**, with a
confidence gate feeding an unmatched **review queue** on the dashboard. The
playlist is a **rolling window** (default 30): the newest add pushes the
oldest live row out.

### Files

| File | Role |
|---|---|
| `src/bot_modules/music_playlist/music_playlist_logic.py` | Pure logic: link extraction, title cleaning, artist inference, match scoring (ported from OpenMusicBot with its test suites) |
| `src/bot_modules/music_playlist/music_playlist_store.py` | SQL over the three tables (window reads, trims, dedupe, ledger, purge) |
| `src/bot_modules/music_playlist/music_playlist_service.py` | The pipeline + `MusicPlaylistSettings` (config KV, `CasinoSettings` pattern, prefix `music_playlist_`) |
| `src/bot_modules/music_playlist/embeds.py` | Pure embed builders for the member panel (accent color passed in) |
| `src/bot_modules/cogs/music_playlist_cog.py` | Thin glue: `on_message`, `on_raw_message_delete`, `/playlist`, source reactions |
| `src/web_server/routes/music_playlist.py` | Admin-gated dashboard API under `/api/music-playlist/*` |
| `src/web_server/static/js/panels/music-playlist.js` | The dashboard panel (route id `music-playlist`, Config → Channels & Messages) |
| `src/migrations/165_music_playlist.sql` | The three tables |

## Settings

`MusicPlaylistSettings` frozen dataclass in the config KV (no settings
table), guild-scoped keys under the `music_playlist_` prefix:

| Field | Default | Notes |
|---|---|---|
| `enabled` | `false` | Doubles as pause — off stops processing, loses nothing |
| `channel_id` | `0` | The one watched channel |
| `playlist_id` | `""` | Spotify playlist id; the settings route also accepts an `open.spotify.com/playlist` link or `spotify:playlist:` URI and normalizes |
| `window_size` | `30` | Dashboard bounds 1–200 |
| `match_threshold` | `0.74` | Confidence gate for search-resolved (YouTube) links |
| `expand_albums` | `false` | Off = album/playlist links are skipped entirely |
| `remove_on_delete` | `true` | Deleting the source message pulls the track |

The owning Spotify account is a config concern (the OAuth grant), not schema
— swapping it is a re-consent, never a migration.

## Pipeline (per message in the watched channel)

1. **Gate:** DMs and bots are ignored; the cog pre-gates on
   `extract_links(content)` being non-empty (linkless messages cost no DB
   read), then the service re-checks `enabled` + `playlist_id` +
   channel match and the processed-message ledger (idempotency).
2. **Parse:** Spotify track links / `spotify:track:` URIs and YouTube video
   links. Spotify album/playlist links and YouTube playlist links are
   **skipped** unless `expand_albums` (they aren't "a song someone posted",
   and one album would flush the window).
3. **Resolve:** direct by id for Spotify tracks; YouTube goes
   oEmbed metadata → cleaned search queries → Spotify search →
   `select_best_match` (title/artist blend with live/remaster mismatch
   penalties, ported from OpenMusicBot's `matching.py`).
4. **Confidence gate:** at or above `match_threshold` the track is added
   silently; below it (or on no metadata / no candidates) the link lands in
   `music_playlist_unmatched` with its best candidate, score, and a reason
   (`no_metadata` / `no_candidates` / `low_confidence`).
5. **Dedupe** against the *window*, not all time: a duplicate records a
   reference row born dead (`removal_reason='duplicate'`) and adds nothing —
   the reference is what keeps deletion honest (below). A song that rolled
   off is postable again.
6. **Write + trim:** survivors are added to the Spotify playlist, then the
   window is trimmed back to `window_size` (oldest live rows marked
   `rolled_off`; trim removals on Spotify are best-effort).
7. **Ledger:** every processed message gets a `music_playlist_messages` row,
   so restarts and re-scans are idempotent. Terminal statuses are `processed`
   and `no_links`; `write_failed` and `resolve_failed` are **retryable** and
   read as unprocessed (see the error contract below). Note the cog's pre-gate
   means genuinely linkless messages are never ledgered — `no_links` only
   lands for link-shaped messages that resolved to nothing.
8. **Reactions** on the source message: ✅ when anything was added, 🔁 when
   any link was a duplicate, 🔁-and-✅ can coexist, ❓ when anything queued.
   A skipped message gets no reaction; a failed `add_reaction` logs and
   never raises.

### Review queue semantics

- `approve_unmatched` flips `pending → approved` as the exactly-once claim;
  a candidate already live in the window becomes a duplicate reference with
  no Spotify write; a **failed Spotify write reopens the item to pending**
  (retryable — surfaced as HTTP 409 on the dashboard).
- `reject_unmatched` flips `pending → rejected`. Reviewed rows are kept as
  queue history.

### Deletions

`on_raw_message_delete` (raw, so uncached old posts still fire). No-op when
`remove_on_delete` is off. A track leaves the playlist **only when no other
live message still references it** — otherwise the surviving reference is
resurrected and the track stays. DB is marked removed first; the Spotify
removal is best-effort (an over-full Spotify playlist is recoverable by
Reconcile; the reverse — a Spotify delete the DB doesn't know about — is
not).

### Error contract (Spotify writes)

Every write catches `SpotifyResolveError`. Missing scope / 403 / 429 set
`write_blocked` and carry the resolver's already-worded message, and nothing
is inserted as live.

**Recovery is re-consent + Re-scan, not Reconcile.** A refused write inserts
no track row, and `reconcile` only pushes tracks the DB already holds, so it
has nothing to work from. Instead the message is ledgered in a *retryable*
status — `write_failed` for a refused write, `resolve_failed` for a link that
errored out during resolution — and `is_message_processed` deliberately reads
those as **unprocessed** so a Re-scan re-fires them. A later successful pass
overwrites the row with its real outcome; a terminal row is never downgraded
back to a failure. Re-processing is safe because anything that did land
dedupes against the live window.

This is the difference between "the read-only window before re-consent costs
you nothing" and "every song posted in it is gone" — the retryable ledger is
what makes the former true.

## Storage (migration 165)

- **`music_playlist_tracks`** — window + history. Removals *mark* rows
  (`removed_at` + `removal_reason` ∈ `rolled_off` / `message_deleted` /
  `duplicate` / `admin`) rather than deleting. Partial unique index on
  `(guild_id, platform, playlist_id, track_id) WHERE removed_at IS NULL` is
  the dedupe rule itself. `platform` defaults `'spotify'` so a second writer
  lands as rows, not a schema rewrite.
- **`music_playlist_unmatched`** — the review queue (partial index on
  pending).
- **`music_playlist_messages`** — the idempotency ledger.

The member column is **`added_by` in both user-data tables** — deliberately
not the plan's `posted_by`, because `added_by` is already in
`privacy_service.SUBJECT_ID_COLUMNS`, so the access export covers both
tables with no code change.

## Member surface

Deliberately thin: the source-message reactions, plus **`/playlist`** — one
ephemeral panel (guarded on `enabled` + `playlist_id`, accent via
`resolve_accent_color`) with three buttons: 🎶 Playlist (the window, newest
first), 🎧 My Songs, ❓ In Review (the caller's pending unmatched links).
No other commands; all admin control is the dashboard.

## Dashboard

Panel `music-playlist` (admin-gated, help id `help-music-playlist`), API
under `/api/music-playlist/`:

| Endpoint | Does |
|---|---|
| `GET /status` | Connection chip (`connected` / `read_only` / `not_connected`, read fresh from the stored grant so re-consent needs no restart) + settings + counts |
| `PUT /settings` | Partial update, 422 on bounds/shape |
| `GET /window`, `DELETE /window/{row_id}` | Live window; admin remove (`removal_reason='admin'`, DB-first, best-effort Spotify) |
| `GET /unmatched`, `POST /unmatched/{id}/approve\|reject` | Review queue |
| `GET /history` | Removed/rolled-off rows with reason (duplicate references excluded) |
| `POST /rescan`, `POST /reconcile` | Delegate to the live cog; 503 when the bot is down |

## Privacy

Both user-data tables have `data_register.md` rows with a **purge**
decision (no Art 17(3) ground to preserve "who posted a song"; the playlist
is keyed by Spotify id, not poster). `music_playlist_store.purge_member_rows`
deletes a member's rows from both tables and nulls `reviewed_by` where they
were the reviewer, and `privacy_service.purge_user_data` calls it as part of
a full erasure. Spotify receives track ids only — never member identity. The
privacy notice (`manual.html` §Your Data & Privacy) carries the collection
line.

## Maintenance actions

The dashboard's two Maintenance buttons delegate to the live cog by name
(503 "bot not connected" when it is down):

- **Re-scan** (`MusicPlaylistCog.rescan_channel`) — replays the pipeline
  over the watched channel's last ``rescan_depth`` messages (default 200,
  1–2000), oldest first, so the window
  ends up holding the newest posts. The processed-message ledger makes it
  idempotent; it exists to catch posts made while the bot was down, **and to
  recover messages left in a retryable status** — which makes it the action
  to run after re-consenting, or after a Spotify outage. No reactions are
  added during a sweep.
- **Reconcile** (`MusicPlaylistCog.reconcile_playlist` →
  `MusicPlaylistService.reconcile`) — squares the actual Spotify playlist
  with the DB's live window, both ways: window tracks missing from Spotify
  are re-added (heals a failed write), Spotify tracks the window doesn't
  hold are removed (heals a best-effort removal that never landed).

  **The removal half is guarded.** "On Spotify but not in the window" also
  describes every song on a playlist the bot never filled, so pointing the
  playlist dial at an existing playlist and clicking Reconcile would strip
  it — irreversibly. Past `_RECONCILE_REMOVAL_CONFIRM_AT` (5) unrecognised
  tracks the adds still run, the removals are withheld, and the result
  carries `needs_confirmation` (`would_remove` plus a 10-id sample) for the
  panel to confirm; `confirm_removals=True` (route:
  `?confirm_removals=true`) then performs them. Note Reconcile is **not**
  the recovery path for a read-only period — Re-scan is, per the error
  contract above.

## Explicitly out (this round)

Apple Music (recommended against in the plan). YouTube as a *write*
destination. Multiple channels/playlists. Per-member posting limits. The
extracted `PlaylistWriter` protocol (waits for a second writer). Ageing-out
of history rows.
