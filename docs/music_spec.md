# Music — Feature Spec

A music playback cog for shared listening in voice channels. Supports YouTube tracks and Spotify URLs (tracks, playlists, albums — resolved to YouTube). Slash commands plus a persistent now-playing card with button controls. A 24/7 mode (always-on channel plus Spotify autoplay-on-idle) existed until 2026-07-28 and was removed entirely — see below.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/play query:<text\|url>` | Slash | Same-voice-channel | Play a search query, a YouTube URL, or a Spotify track/playlist/album URL |
| `/skip` | Slash | Same-voice-channel | Skip the current track |
| `/queue [page]` | Slash | Everyone | Show current track + upcoming queue (paginated) |
| `/shuffle` | Slash | Same-voice-channel | Shuffle the upcoming queue (does not interrupt the current track) |
| `/loop mode:<off\|track\|queue>` | Slash | Same-voice-channel | Set the loop mode |
| `/pause` / `/resume` | Slash | Same-voice-channel | Pause or resume playback |
| `/stop` | Slash | Same-voice-channel | Clear the queue and disconnect |
| `/nowplaying` | Slash | Everyone | Repost the now-playing card |
| `/disconnect` | Slash | Same-voice-channel | Force-disconnect from voice |

The now-playing card is a persistent message with five buttons: **Pause/Resume**, **Skip**, **Stop**, **Shuffle**, and **Loop** (cycles off → track → queue). Buttons require the clicker to be in the same voice channel as the bot.

## Behavior

### `/play`

Joins the invoker's voice channel if the bot isn't already connected there. If the bot is connected to a different channel in the same guild, the command is rejected. Resolution:

- **Search query** → looks up on YouTube, queues the top match.
- **YouTube URL** → queues directly.
- **Spotify track URL** → resolved to YouTube via ISRC where available, falling back to a title+artist search.
- **Spotify playlist or album URL** → resolves up to 500 tracks (warns if the playlist is larger), queues them in order.

If the queue is empty, playback starts immediately; otherwise the new tracks are enqueued and the user gets an ephemeral "queued" confirmation.

### Now-playing card

On track start, the bot posts a card to the text channel where `/play` was invoked: title (linked), artist, requester, duration, position in queue, current loop mode, artwork thumbnail. Subsequent track starts edit the same card rather than spamming new posts. The card's buttons stay live across bot restarts.

### 24/7 mode (removed 2026-07-28)

The bot could be pinned into one voice channel per guild, exempt from the idle
disconnect, optionally re-shuffling 50 tracks from a Spotify playlist whenever
the queue emptied. It was removed whole in the command-surface audit: the two
commands (`/247`, `/247_status`), the always-on rejoin on startup, the
idle-disconnect exemption, the autoplay refill, and the per-channel settings
table.

Autoplay went with it because it was never independent — it only ever ran for a
channel that was already always-on, so there was nothing left to trigger it.

One production channel was pinned at the time, with an autoplay playlist set.
Removing this means the bot leaves that channel on the normal idle sweep like
any other; the empty `music_channel_settings` table is left in place rather than
dropped by migration.

### Track-failure fallback (added 2026-07-30)

YouTube refuses some tracks outright — most commonly a rights-holder content
claim (e.g. "blocked due to the claimed content by SME") that no client
rotation can bypass. When the current track raises a Lavalink exception, the
cog attempts **one** recovery before giving up:

1. Search YouTube for an alternate upload (excluding the failed video id),
   then SoundCloud (`scsearch:`, requires `soundcloud: true` in
   `lavalink/application.yml`). Each source tries two queries: title +
   uploader first, then the bare title — a YouTube "author" is the uploader
   channel, which for re-uploads is unrelated to the song and poisons the
   search (verified against the real 2026-07-30 failures).
2. Each candidate must pass the guard in `music/logic.py::pick_substitute`:
   duration within ±20% of the original (floor ±15s), ≥60% of the original
   title's core words present, and no variant term (cover / remix / sped up /
   slowed / nightcore / live / "1 hour" / etc.) the original title didn't
   already contain.
3. The first survivor plays, with a visible one-line note — substitution is
   never silent: "⚠️ Couldn't play **X** — it's blocked by the rights holder
   on YouTube. Playing the closest match from SoundCloud instead: **Y**."
   The requester carries over to the substitute's now-playing card.
4. If no candidate survives (or the substitute itself fails — recovery never
   loops), the plain-language failure line posts and the queue advances.

`on_wavelink_track_end` only advances the queue on reason `finished`:
Lavalink also ends tracks with `replaced` (/skip, substitutes), `loadFailed`
(owned by the exception handler), `stopped`, and `cleanup`, and advancing on
those double-advanced the queue (a /skip with ≥3 queued tracks dropped one).

### Spotify URL handling

Track, playlist, and album URLs from `open.spotify.com` and `spotify:` URIs are recognized. Playlists cap at 500 tracks per submission. Tracks that can't be matched on YouTube (no ISRC, no clear search match) are skipped with a warning, not a hard failure.

## Permissions

- **Bot:** Connect and Speak in the target voice channels; Send Messages and Embed Links in the text channels where the now-playing card posts.
- **User:** Must be in a voice channel for `/play`. For playback-control commands (skip, shuffle, loop, pause, resume, stop, disconnect) and now-playing buttons, must be in the same voice channel as the bot.

## User-visible errors

| When | The user sees |
|---|---|
| User runs `/play` while not in voice | "Join a voice channel first." |
| User runs `/play` while in a different channel from the bot | "I'm currently in #other-channel. Join me there or wait for the queue to finish." |
| Spotify playlist exceeds 500 tracks | "Playlist is X tracks; queued the first 500." |
| YouTube track fails to load (rights-holder block, age gate, removed, etc.) | The bot tries one substitute (alternate YouTube upload, then SoundCloud) and announces it: "⚠️ Couldn't play **X** — [reason]. Playing the closest match from SoundCloud instead: **Y**." If nothing passes the match guard, one plain line naming the track and the reason posts and the next track plays. Full Lavalink exception goes to the log only. |
| Spotify URL is private or doesn't exist | "Playlist is private or doesn't exist." |
| Spotify URL is malformed | "Not a valid Spotify URL." |
| Now-playing button clicked from outside the voice channel | "You need to be in the voice channel." |
| Music backend isn't running | Cog fails to load with a clear error; the rest of the bot keeps running |

## Non-goals

- Apple Music, Deezer, Tidal sources. SoundCloud is enabled as the fallback
  when YouTube refuses a track; a pasted SoundCloud URL therefore also plays,
  but SoundCloud *search* stays YouTube-first and isn't a promoted feature.
- Saved community playlists / named presets.
- Lyrics, audio filters, EQ.
- Vote-skip, per-user request limits.
- Persistent queue across restarts (queue is in-memory only).

## Configuration

The music cog has no per-guild configuration of its own.

### Audio quality and Opus passthrough (2026-08-16)

Lavalink forwards the source's Opus frames to Discord untouched — no decode, no
re-encode, near-zero CPU — but **only** while the player's volume is exactly 100
and no filter is active. Any other volume forces a full decode → resample →
re-encode, which adds a lossy generation on top of a source that is usually
already Opus (YouTube itag 251, ~149kbps).

`_DEFAULT_VOLUME` in `cogs/music_cog.py` is therefore **100 and must stay there**.
It was 20 until 2026-08-16, chosen so the bot wasn't startling on connect; the
cost turned out to be every track being re-encoded, on a voice channel running at
384kbps. Members who want it quieter use Discord's own per-user volume slider.

The Lavalink dials live in `lavalink/application.yml`:

| Setting | Value | Why |
|---|---|---|
| `bufferDurationMs` | 1000 | Koe's UDP send buffer. The 400 default leaves no headroom for a GC pause or network hiccup, both of which are audible as stutter. Raising it slows skip/seek response, not quality. |
| `frameBufferDurationMs` | 10000 | Source-side pre-buffer; guards against YouTube throttling mid-track. Costs heap per player. |
| `opusEncodingQuality` | 10 | Already the maximum. |
| `resamplingQuality` | HIGH | Only in the path when passthrough is off (non-Opus sources such as the SoundCloud fallback, or YouTube tracks offering only AAC). Free on tracks that pass through. |

Lavalink exposes no encode-bitrate setting; passthrough on or off is effectively
the quality switch.

Lavalink runs as a **child process of the bot** (`services/lavalink_manager.py`),
not a systemd unit, with its heap from `LAVALINK_HEAP_MB` (2048 in prod; the 512
default sat at ~72% resident, which means frequent GC and audible stutter). That
variable is read when the bot constructs the manager, so a heap change needs a
full bot restart — respawning the JVM alone only picks up `application.yml`.

## Stored data

No persistent tables. The per-channel 24/7 settings table (`music_channel_settings`) is no longer read or written; it survives as an empty orphan. All queue state, playback position, and now-playing message ids are in-memory only and don't survive a restart.
