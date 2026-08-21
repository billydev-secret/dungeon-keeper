# Music Playlist — a watched channel becomes a rolling Spotify playlist

**Reference spec.** Describes the feature as built (2026-08-17, defect fixes
2026-08-18). The design history, the port analysis of OpenMusicBot, and the
platform scoping (YouTube-as-destination, Apple Music) live in the plan:
[plans/music-playlist-cog.md](plans/music-playlist-cog.md). Where that plan
left questions open, this build resolved them:

1. An album or playlist link contributes exactly its **single most-popular
   track** (2026-08-18, superseding the launch build's skip-behind-a-dial;
   the `expand_albums` dial is retired, migration 170 sweeps the key).
   `popularity` is Spotify's 0-100 recency-weighted score, *not* a play
   count — and while the app is in Development mode Spotify nulls it
   everywhere, so every track ties and the tie-break (disc, then track
   number; playlist picks tie-break on track number) selects the opening
   track. The pick self-upgrades to genuinely popularity-ranked when the
   app's quota extension is granted, with no code change.
2. Link parsing covers **Spotify + YouTube only** (no Apple Music /
   SoundCloud parsing this round).
3. Rolled-off history rows are **kept** — no ageing-out yet.
4. Source-message reactions are **on**: ✅ added, 🔁 duplicate, ❓ queued.

**Development-mode caveat (as of 2026-08-18):** the Spotify app has no quota
extension, and Spotify returns `track: null` for every playlist item read,
403s the batched `GET /v1/tracks`, and nulls `popularity` on every surface.
The resolver refuses to treat an all-null playlist read as an empty playlist
(`SpotifyUnusableReadError`) — that guard is what keeps Reconcile from
re-adding its whole window — and a playlist *link* posted in the channel
lands in the review queue as `collection_unreadable`. Album reads
(`album_tracks`) work.

**Activation:** the feature is inert until (a) the service restarts with this
code, and (b) the bot's Spotify account re-consents at `/spotify/authorize` —
the pre-existing grant is read-only and the widened
`SPOTIFY_SCOPES` (`routes/spotify_oauth.py:34`, now including
`playlist-modify-private playlist-modify-public`) only take effect on a fresh
consent. The dashboard's connection chip reports this state in words
(read-only → "needs re-consent") rather than surfacing a bare 403.

## Shape

One listener on a single dashboard-picked channel. Each message with music
links runs the pipeline: **parse → resolve → dedupe → write → trim**; links
the resolver has nothing to add for feed an unmatched **review queue** on
the dashboard, whose verdicts are remembered per link. The playlist is a
**rolling window** (default 30): the newest add pushes the oldest live row
out.

### Files

| File | Role |
|---|---|
| `src/bot_modules/music_playlist/music_playlist_logic.py` | Pure logic: link extraction, title cleaning, artist inference, match scoring (ported from OpenMusicBot with its test suites) |
| `src/bot_modules/music_playlist/music_playlist_store.py` | SQL over the three tables (window reads, trims, dedupe, ledger, purge) |
| `src/bot_modules/music_playlist/music_playlist_service.py` | The pipeline + `MusicPlaylistSettings` (config KV, `CasinoSettings` pattern, prefix `music_playlist_`) |
| `src/bot_modules/music_playlist/embeds.py` | Pure embed builders for the member panel (accent color passed in) |
| `src/bot_modules/cogs/music_playlist_cog.py` | Thin glue: `on_message`, `on_raw_message_delete`, `/playlist`, source reactions, the 5-minute retry sweep loop |
| `src/web_server/routes/music_playlist.py` | Admin-gated dashboard API under `/api/music-playlist/*` |
| `src/web_server/static/js/panels/music-playlist.js` | The dashboard panel (route id `music-playlist`, Config → Channels & Messages) |
| `src/migrations/165_music_playlist.sql` | The three tables (169 adds the ledger's `attempts`; 170 sweeps the retired dial key) |

## Settings

`MusicPlaylistSettings` frozen dataclass in the config KV (no settings
table), guild-scoped keys under the `music_playlist_` prefix:

| Field | Default | Notes |
|---|---|---|
| `enabled` | `false` | Doubles as pause — off stops processing, loses nothing |
| `channel_id` | `0` | The one watched channel |
| `playlist_id` | `""` | Spotify playlist id; the settings route also accepts an `open.spotify.com/playlist` link or `spotify:playlist:` URI and normalizes |
| `window_size` | `30` | Dashboard bounds 1–200 |
| `remove_on_delete` | `true` | Deleting the source message pulls the track |
| `rescan_depth` | `200` | How far back Re-scan reads (dashboard bounds 1–2000) |

(Two launch dials were retired 2026-08-18, their stored keys swept by
migration: `expand_albums` — collection links now always contribute one
track, migration 170 — and `match_threshold` — the best-scoring candidate is
now always added, so a confidence gate would be unenforced, migration 171.)

The owning Spotify account is a config concern (the OAuth grant), not schema
— swapping it is a re-consent, never a migration.

**Repointing `playlist_id` strands the back catalogue, by design.** The
processed-message ledger is playlist-agnostic, so messages already ledgered
terminal never re-process into the new playlist, and nothing already added
moves. Decided 2026-08-18 (warning over re-keying the ledger): the panel
confirms a playlist change with exactly this caveat, and the field hint
carries it statically. The 22 tracks the first Re-scan put into the
previously-configured playlist are the incident that surfaced this.

## Pipeline (per message in the watched channel)

1. **Gate:** DMs and bots are ignored; the cog pre-gates on
   `extract_links(content)` being non-empty (linkless messages cost no DB
   read), then the service re-checks `enabled` + `playlist_id` +
   channel match and the processed-message ledger (idempotency).
2. **Parse:** Spotify track links / `spotify:track:` URIs and YouTube video
   links. A Spotify album or playlist link contributes its single
   most-popular track (`SpotifyResolver.album_top_track` /
   `playlist_top_track`; album popularity needs a batched `GET /v1/tracks`
   pass, cached per album id) — one collection post can never flush the
   window. A collection the app cannot read — an editorial (`37i9…`)
   playlist, or the Development-mode null-track shape — raises
   `SpotifyUnusableReadError` and is queued for review as
   `collection_unreadable` (terminal, not retried: the condition is
   lasting).
3. **Resolve:** direct by id for Spotify tracks; YouTube goes
   oEmbed metadata → cleaned search queries → Spotify search →
   `select_best_match` (title/artist blend with live/remaster mismatch
   penalties, ported from OpenMusicBot's `matching.py`) — and the
   **best-scoring candidate is always added** (Billy, 2026-08-18: a wrong
   pick in a rolling window is cheap; the scoring still decides *which*
   candidate wins). Before any of that, **reviewer verdicts are remembered
   per link**: a URL with a resolved review row reuses that answer — an
   approved link re-adds the reviewer's exact track with no fetch at all,
   a rejected link contributes nothing and never re-queues.
4. **Review queue:** only links with nothing to add reach
   `music_playlist_unmatched` — no metadata
   (`youtube_metadata_unavailable`), no candidates
   (`no_spotify_candidates`), or an unreadable collection
   (`collection_unreadable`, no candidate — reject is the only resolution).
   `confidence_below_threshold` appears on historical rows only.
5. **Dedupe** against the *window*, not all time: a duplicate records a
   reference row born dead (`removal_reason='duplicate'`) and adds nothing —
   the reference is what keeps deletion honest (below). A song that rolled
   off is postable again.
6. **Write + trim:** survivors are added to the Spotify playlist, then the
   window is trimmed back to `window_size` (oldest live rows marked
   `rolled_off`; trim removals on Spotify are best-effort).
7. **Ledger:** every processed message gets a `music_playlist_messages` row,
   so restarts and re-scans are idempotent. Terminal statuses are `processed`,
   `no_links`, and `message_gone` (the retry sweep found the message deleted);
   `write_failed` and `resolve_failed` are **retryable** and
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
  queue history — and as **verdict memory**
  (`store.latest_review_verdict`, newest resolved row per guild+URL): any
  re-processing of that link, or the same URL posted again, reuses the
  reviewer's answer instead of re-asking. Pending rows are the open
  question and don't count.
- Migration 171 (2026-08-18, the policy change) re-fired every message that
  had a pending queue row and deleted the pending rows: remembered approvals
  re-added the reviewer's exact tracks, the rest added their best candidate.

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
is inserted as live. Transport failures are part of the same contract:
spotipy speaks requests, so a connection reset arrives as
`requests.exceptions.ConnectionError` — `SpotifyResolver._call` retries it on
the 429 backoff ladder and exhausts into `SpotifyResolveError` (2026-08-18;
before that it escaped unwrapped and the song vanished with no ledger row,
reaction, or queue entry). The httpx token-refresh path wraps the same way so
a blip there degrades to the client-credentials fallback.

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

### The auto-retry sweep (2026-08-18)

Retryable rows no longer wait for a human: a 5-minute `tasks.loop` on the cog
visits every guild holding one (`guilds_with_retryable`) and re-runs each
*due* message through `process_message`. Policy constants live in the
service: exponential backoff from the latest failure (due at
`processed_at + 300s × 2^attempts`; migration 169 adds the `attempts`
column), batch cap 10 per guild per sweep, giving up after 8 attempts (~10h)
— past the cap the row stays retryable and a manual Re-scan (which ignores
`attempts`) remains the recovery for long outages like a consent gap.
Attempts bump *before* re-processing so a crash mid-retry cannot hot-loop. A
message the fetch finds deleted ledgers terminally as `message_gone`; other
fetch failures leave the row for a later sweep. The sweep is **write-side
only — no playlist read is involved**, which is why automating Reconcile
instead would have been wrong (see the read guard below). Unlike Re-scan, a
successful retry delivers the source-message reaction the original failure
swallowed.

## Storage (migrations 165, 169–171)

- **`music_playlist_tracks`** — window + history. Removals *mark* rows
  (`removed_at` + `removal_reason` ∈ `rolled_off` / `message_deleted` /
  `duplicate` / `admin`) rather than deleting. Partial unique index on
  `(guild_id, platform, playlist_id, track_id) WHERE removed_at IS NULL` is
  the dedupe rule itself. `platform` defaults `'spotify'` so a second writer
  lands as rows, not a schema rewrite.
- **`music_playlist_unmatched`** — the review queue (partial index on
  pending).
- **`music_playlist_messages`** — the idempotency ledger; migration 169 adds
  `attempts` (the retry sweep's counter and backoff exponent). Migrations
  170 and 171 are data-only: each deletes a retired dial's config key
  (`expand_albums`, `match_threshold`), and 171 also re-fired the pending
  review backlog through the best-effort pipeline (see the review queue
  section).

The member column is **`added_by` in both user-data tables** — deliberately
not the plan's `posted_by`, because `added_by` is already in
`privacy_service.SUBJECT_ID_COLUMNS`, so the access export covers both
tables with no code change.

## Member surface

Deliberately thin: the source-message reactions, plus **`/playlist`** — one
ephemeral panel (guarded on `enabled` + `playlist_id`, accent via
`safe_resolve_accent`) with three buttons: 🎶 Playlist (the window, newest
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

  **The read half is guarded too (2026-08-18).** Spotify can report a
  playlist's items while nulling every `track` object (the Development-mode
  shape); treating that as "the playlist is empty" made Reconcile compute
  `missing = the whole window` and re-add it once per click.
  `playlist_track_ids` now raises `SpotifyUnusableReadError` on an all-null
  read, so Reconcile reports the error and changes nothing. An all-local
  playlist (real track objects, no catalog ids) still reads as empty-but-
  readable. Either way the extras set is computed from what Spotify
  returned, so a blind read can only ever have duplicated — never deleted.

## Explicitly out (this round)

Apple Music (recommended against in the plan). YouTube as a *write*
destination. Multiple channels/playlists. Per-member posting limits. The
extracted `PlaylistWriter` protocol (waits for a second writer). Ageing-out
of history rows.
