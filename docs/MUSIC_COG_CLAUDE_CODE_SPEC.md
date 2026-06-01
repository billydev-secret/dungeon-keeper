# Music — Feature Spec

A music playback cog for shared listening in voice channels. Supports YouTube tracks and Spotify URLs (tracks, playlists, albums — resolved to YouTube). Slash commands plus a persistent now-playing card with button controls. Optional 24/7 mode keeps the bot in a voice channel and auto-queues from a Spotify playlist when idle.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/play query:<text\|url>` | Slash | Same-voice-channel | Play a search query, a YouTube URL, or a Spotify track/playlist/album URL |
| `/skip` | Slash | Same-voice-channel | Skip the current track |
| `/queue [page]` | Slash | Everyone | Show current track + upcoming queue (paginated) |
| `/shuffle` | Slash | Same-voice-channel | Shuffle the upcoming queue (does not interrupt the current track) |
| `/loop mode:<off\|track\|queue>` | Slash | Same-voice-channel | Set the loop mode |
| `/pause` / `/resume` | Slash | Same-voice-channel | Pause or resume playback |
| `/stop` | Slash | Same-voice-channel | Clear the queue; disconnect unless 24/7 is on |
| `/nowplaying` | Slash | Everyone | Repost the now-playing card |
| `/disconnect` | Slash | Same-voice-channel | Force-disconnect from voice |
| `/247 enabled:<bool> [autoplay_playlist:<spotify_url>]` | Slash | Mod | Toggle 24/7 mode for the invoker's current voice channel, with optional Spotify autoplay |
| `/247_status` | Slash | Mod | Show channels with 24/7 enabled in this guild |

The now-playing card is a persistent message with five buttons: **Pause/Resume**, **Skip**, **Stop**, **Shuffle**, and **Loop** (cycles off → track → queue). Buttons require the clicker to be in the same voice channel as the bot.

## Behaviour

### `/play`

Joins the invoker's voice channel if the bot isn't already connected there. If the bot is connected to a different channel in the same guild, the command is rejected. Resolution:

- **Search query** → looks up on YouTube, queues the top match.
- **YouTube URL** → queues directly.
- **Spotify track URL** → resolved to YouTube via ISRC where available, falling back to a title+artist search.
- **Spotify playlist or album URL** → resolves up to 500 tracks (warns if the playlist is larger), queues them in order.

If the queue is empty, playback starts immediately; otherwise the new tracks are enqueued and the user gets an ephemeral "queued" confirmation.

### Now-playing card

On track start, the bot posts a card to the text channel where `/play` was invoked: title (linked), artist, requester, duration, position in queue, current loop mode, artwork thumbnail. Subsequent track starts edit the same card rather than spamming new posts. The card's buttons stay live across bot restarts.

### 24/7 mode

Per voice channel (one channel per guild can be designated). When enabled, the bot:

- Stays connected to that voice channel when the queue empties or when alone — does **not** auto-disconnect.
- If an **autoplay playlist** is configured for that channel, re-resolves and re-shuffles 50 tracks from it whenever the queue empties; continues indefinitely.
- Rejoins on bot restart (joins within ~60 s of cog load).
- If the autoplay playlist becomes private or deleted, autoplay pauses with a notice in the last-used text channel but the bot stays in voice.

Without 24/7, the bot disconnects after 60 s alone in the channel and after the queue empties.

### Spotify URL handling

Track, playlist, and album URLs from `open.spotify.com` and `spotify:` URIs are recognised. Playlists cap at 500 tracks per submission. Tracks that can't be matched on YouTube (no ISRC, no clear search match) are skipped with a warning, not a hard failure.

## Permissions

- **Bot:** Connect and Speak in the target voice channels; Send Messages and Embed Links in the text channels where the now-playing card posts.
- **User:** Must be in a voice channel for `/play`. For playback-control commands (skip, shuffle, loop, pause, resume, stop, disconnect) and now-playing buttons, must be in the same voice channel as the bot. `/247` and `/247_status` require Mod.

## User-visible errors

| When | The user sees |
|---|---|
| User runs `/play` while not in voice | "Join a voice channel first." |
| User runs `/play` while in a different channel from the bot | "I'm currently in #other-channel. Join me there or wait for the queue to finish." |
| Spotify playlist exceeds 500 tracks | "Playlist is X tracks; queued the first 500." |
| YouTube track fails to load (region block, removed, etc.) | Skipped silently, next track plays |
| Spotify URL is private or doesn't exist | "Playlist is private or doesn't exist." |
| Spotify URL is malformed | "Not a valid Spotify URL." |
| Now-playing button clicked from outside the voice channel | "You need to be in the voice channel." |
| Music backend isn't running | Cog fails to load with a clear error; the rest of the bot keeps running |

## Non-goals

- Apple Music, Deezer, Tidal, SoundCloud sources.
- Saved community playlists / named presets.
- Lyrics, audio filters, EQ.
- Vote-skip, per-user request limits.
- Persistent queue across restarts (queue is in-memory only).
- Multiple 24/7 channels per guild simultaneously (one per guild).

## Configuration

24/7 mode is per voice channel, set by mods through `/247`. Each entry stores:

- The voice channel it applies to.
- Whether 24/7 is on.
- An optional Spotify playlist URL for autoplay-on-idle.

No per-guild config beyond that — all other behaviour (cooldowns, queue caps, the now-playing card's appearance) is fixed by code.

## Stored data

One per-channel table for 24/7 settings (voice channel id, always-on flag, optional autoplay playlist URL, updater id, last-updated timestamp). All queue state, playback position, and now-playing message ids are in-memory only and don't survive a restart.
