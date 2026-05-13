# Dungeon Keeper – Music Cog (Claude Code Spec)

## 1. Overview

Add a music playback cog to the existing Dungeon Keeper Discord bot. The cog manages a Lavalink subprocess for audio streaming, supports YouTube and Spotify (Spotify via metadata-only resolution to YouTube), and provides slash commands plus a now-playing embed with interactive controls.

**Primary library:** [`wavelink`](https://github.com/PythonistaGuild/Wavelink) v3.x (discord.py-compatible Lavalink client).

**Audio backend:** [Lavalink v4](https://github.com/lavalink-devs/Lavalink) with the [LavaSrc](https://github.com/topi314/LavaSrc) plugin for Spotify support.

## 2. Goals

- Members can play YouTube tracks, Spotify tracks, and Spotify playlists in voice channels.
- Robust queue management (add, skip, shuffle, loop track, loop queue, clear, view).
- Now-playing embed with button controls (pause/resume, skip, stop, shuffle, loop).
- Bot lifecycle manages Lavalink: starts on cog load, gracefully shuts down on cog unload or bot exit.
- Clean failure modes when Lavalink is unavailable, tracks fail to resolve, or voice connection drops.

## 3. Non-goals (v1)

- Apple Music, Deezer, Tidal, SoundCloud (LavaSrc supports them; disabled in config for v1).
- Saved community playlists.
- Lyrics, filters, EQ, vote-skip, request limits.

## 4. File structure

```
dungeon_keeper/
├── cogs/
│   └── music/
│       ├── __init__.py
│       ├── music.py              # MusicCog (main cog class)
│       ├── lavalink_manager.py   # Subprocess lifecycle: start, health-check, shutdown
│       ├── spotify_resolver.py   # Spotify API → list of search queries
│       ├── queue_manager.py      # Per-guild queue state, loop modes
│       ├── now_playing.py        # NowPlayingView (discord.ui.View) + embed builder
│       ├── settings_store.py     # SQLite-backed per-guild settings (24/7 channels, etc.)
│       └── constants.py          # Config keys, timeouts, colors, emoji
├── lavalink/
│   ├── Lavalink.jar              # Downloaded by setup script (gitignored)
│   ├── application.yml           # Lavalink config (committed; secrets via env)
│   ├── plugins/                  # LavaSrc jar lives here (gitignored)
│   └── logs/                     # Lavalink logs (gitignored)
└── scripts/
    └── setup_lavalink.sh         # Downloads Lavalink.jar + LavaSrc plugin
```

## 5. Dependencies

Add to `requirements.txt`:

```
wavelink>=3.4.0
spotipy>=2.23.0
```

System requirements:
- Java 17 or newer on `PATH`
- ~512 MB RAM for Lavalink
- Outbound HTTPS to Spotify, YouTube

## 6. Configuration

### 6.1 Environment variables (add to existing Dungeon Keeper `.env`)

```
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
LAVALINK_PASSWORD=<random_string>  # also referenced in application.yml
LAVALINK_PORT=2333                 # default; only change if conflict
LAVALINK_HOST=127.0.0.1
LAVALINK_HEAP_MB=512               # JVM -Xmx
```

### 6.2 `lavalink/application.yml`

Standard Lavalink v4 config with LavaSrc enabled. Key sections:

```yaml
server:
  port: ${LAVALINK_PORT:2333}
  address: 127.0.0.1

lavalink:
  plugins:
    - dependency: "com.github.topi314.lavasrc:lavasrc-plugin:4.x.x"
      repository: "https://maven.lavalink.dev/releases"
  server:
    sources:
      youtube: true
      bandcamp: false
      soundcloud: false
      twitch: false
      vimeo: false
      http: false
      local: false
    password: ${LAVALINK_PASSWORD}

plugins:
  lavasrc:
    providers:
      - "ytsearch:\"%ISRC%\""
      - "ytsearch:%QUERY%"
    sources:
      spotify: true
      applemusic: false
      deezer: false
      yandexmusic: false
    spotify:
      clientId: ${SPOTIFY_CLIENT_ID}
      clientSecret: ${SPOTIFY_CLIENT_SECRET}
      countryCode: US
```

Note: pin the LavaSrc version explicitly when implementing — check latest stable on the releases page.

## 7. Component specs

### 7.1 `lavalink_manager.py`

Class: `LavalinkManager`

**Responsibilities:** start Lavalink subprocess, wait for it to bind its port, expose health-check, shut it down cleanly.

**Methods:**

- `async def start() -> None`
  - Builds command: `java -Xmx{LAVALINK_HEAP_MB}M -jar lavalink/Lavalink.jar`
  - Spawns via `asyncio.create_subprocess_exec`, captures stdout/stderr to `lavalink/logs/lavalink.log` (rotate at 10 MB, keep 3).
  - Polls `127.0.0.1:LAVALINK_PORT` with TCP connect attempts every 0.5s for up to 30s.
  - Raises `LavalinkStartupError` on timeout.

- `async def stop() -> None`
  - Sends SIGTERM, waits 10s.
  - SIGKILL if still alive.
  - Logs exit code.

- `async def is_alive() -> bool`
  - Returns True if subprocess.returncode is None.

- `async def health_check() -> dict`
  - Returns `{"alive": bool, "pid": int, "uptime_s": float}`.

**Lifecycle hooks:**
- Called from `MusicCog.cog_load()` (start)
- Called from `MusicCog.cog_unload()` (stop)
- Bot's `on_close` should also call `stop()` as a safety net

### 7.2 `spotify_resolver.py`

Class: `SpotifyResolver`

Initialized with `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`. Uses `spotipy.Spotify` with `SpotifyClientCredentials` auth (no user OAuth needed — public metadata only).

**Methods:**

- `def is_spotify_url(s: str) -> bool` — matches `open.spotify.com/track/...`, `open.spotify.com/playlist/...`, `open.spotify.com/album/...`, and `spotify:track:...`/`spotify:playlist:...`/`spotify:album:...` URIs.

- `async def resolve(url: str) -> SpotifyResolveResult`
  - Returns dataclass with `kind` (`"track" | "playlist" | "album"`), `name` (playlist/album name or `None`), `tracks: list[SpotifyTrack]`.
  - `SpotifyTrack` has: `title`, `artists` (list[str]), `duration_ms`, `isrc` (optional), `spotify_url`.

- `def to_search_query(track: SpotifyTrack) -> str`
  - Prefer ISRC search: `f'ytsearch:"{track.isrc}"'` if ISRC present.
  - Fallback: `f"ytsearch:{title} {primary_artist}"`.
  - Note: actual ISRC handling is done by LavaSrc via the providers config; this method is the bot-side fallback if LavaSrc Spotify mirroring is bypassed.

**Limits:**
- Spotify playlist endpoint paginates 100 tracks per call. Resolve up to **500 tracks** for v1 (configurable constant), warn user if playlist is larger.
- Albums: full track list, no cap.

**Error handling:**
- Invalid URL → `SpotifyResolveError("Not a valid Spotify URL")`
- Private playlist or 404 → `SpotifyResolveError("Playlist is private or doesn't exist")`
- Spotify API rate limit (429) → exponential backoff up to 3 retries

### 7.3 `queue_manager.py`

Class: `GuildQueue`

Per-guild state. Stored in a `dict[int, GuildQueue]` keyed by `guild_id` on the cog.

**State:**
- `tracks: collections.deque[wavelink.Playable]`
- `current: wavelink.Playable | None`
- `loop_mode: LoopMode` (enum: `OFF`, `TRACK`, `QUEUE`)
- `history: collections.deque` (last 50 played, for "previous" if added later)
- `voice_channel_id: int | None`
- `text_channel_id: int | None` (where commands were issued, for now-playing posts)

**Methods:**
- `add(track)` / `add_many(tracks)`
- `next() -> Playable | None` — respects loop_mode
- `skip()` — pops current, returns next
- `shuffle()` — shuffles `tracks`, not `current`
- `clear()`
- `set_loop(mode)`
- `peek(n=10) -> list[Playable]`
- `position(track_index) -> int` — for queue display

### 7.4 `settings_store.py`

Class: `MusicSettingsStore`

Persistent per-guild and per-channel music settings. Uses `aiosqlite` (already in Dungeon Keeper's stack per existing convention).

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS music_channel_settings (
    guild_id INTEGER NOT NULL,
    voice_channel_id INTEGER NOT NULL,
    always_on INTEGER NOT NULL DEFAULT 0,        -- 0/1 boolean: 24/7 mode
    autoplay_playlist_url TEXT,                  -- optional Spotify playlist URL for autoplay
    last_updated_ts INTEGER NOT NULL,
    updated_by_user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, voice_channel_id)
);

CREATE INDEX IF NOT EXISTS idx_music_always_on
    ON music_channel_settings(guild_id, always_on)
    WHERE always_on = 1;
```

**Methods:**
- `async def get_channel_settings(guild_id, channel_id) -> ChannelSettings | None`
- `async def set_always_on(guild_id, channel_id, enabled, user_id) -> None`
- `async def set_autoplay_playlist(guild_id, channel_id, playlist_url, user_id) -> None`
- `async def list_always_on_channels(guild_id) -> list[ChannelSettings]`
- `async def list_all_always_on() -> list[ChannelSettings]` — used at startup to rejoin

DB path: same SQLite file Dungeon Keeper already uses (confirm path with implementer); table prefix `music_` keeps it namespaced.

### 7.5 `now_playing.py`

Function: `build_embed(track, queue, requester) -> discord.Embed`
- Title: track title (linked to source URL)
- Author: artist
- Thumbnail: track artwork if available
- Fields: requested by, duration, position in queue, loop mode
- Color: warm gold (`0xC9A961`) — fits Golden Meadow palette

Class: `NowPlayingView(discord.ui.View)`
- Persistent view (timeout=None) — survives bot restarts via `bot.add_view()` registration on startup.
- Buttons (single row):
  - ⏯️ Pause/Resume (toggles label)
  - ⏭️ Skip
  - ⏹️ Stop (clears queue, disconnects)
  - 🔀 Shuffle
  - 🔁 Loop (cycles OFF → TRACK → QUEUE → OFF; emoji changes)
- Permission check on each button: user must be in the same voice channel as the bot. If not, ephemeral "You need to be in the voice channel" response.
- Updates the embed in-place after each action via `interaction.response.edit_message`.

### 7.6 `music.py` (MusicCog)

discord.py Cog with slash commands. Uses `app_commands`.

**Cog lifecycle:**
- `cog_load`: instantiate `LavalinkManager`, start it, then connect wavelink with `wavelink.Pool.connect(...)`. Initialize `MusicSettingsStore`. Register `NowPlayingView` for persistence. Schedule `_rejoin_always_on_channels()` as a background task after wavelink connects (don't block cog_load on it).
- `cog_unload`: cancel rejoin task if pending, disconnect all wavelink players, then `lavalink_manager.stop()`.
- `_rejoin_always_on_channels()`: queries `settings_store.list_all_always_on()`, joins each voice channel, starts autoplay if configured. Resilient to individual channel failures (logs and continues).

**Slash commands:**

| Command | Args | Behavior |
|---|---|---|
| `/play` | `query: str` | Joins user's voice channel if not connected. Resolves query (Spotify URL → spotify_resolver; otherwise wavelink search). Adds to queue. Plays if idle, else confirms enqueue. |
| `/skip` | — | Skips current track. |
| `/queue` | `page: int = 1` | Embed with current + next 10 tracks. Pagination if needed. |
| `/shuffle` | — | Shuffles queue (not current track). |
| `/loop` | `mode: Literal["off","track","queue"]` | Sets loop mode. |
| `/stop` | — | Clears queue. If 24/7 is on, stays in voice; otherwise disconnects. |
| `/nowplaying` | — | Reposts the now-playing embed. |
| `/pause` | — | Pauses playback. |
| `/resume` | — | Resumes playback. |
| `/disconnect` | — | Force-disconnects from voice. Disables 24/7 for that channel if it was on (with confirmation). |
| `/247` | `enabled: bool, autoplay_playlist: str = None` | **Mod-only.** Toggles 24/7 mode for the user's current voice channel. Optional Spotify playlist URL for autoplay when queue is idle. |
| `/247_status` | — | Shows which channels in this guild have 24/7 enabled. |

**Permissions:**
- All commands require user to be in a voice channel.
- `/stop`, `/skip`, `/shuffle`, `/loop`, `/pause`, `/resume`, `/disconnect`: require user be in the SAME voice channel as the bot.
- `/247` and `/247_status`: require Manage Channels permission OR a configured "music mod" role (reuse Dungeon Keeper's existing mod role check pattern — confirm with implementer).

**Wavelink event handlers:**

- `on_wavelink_track_start`: post now-playing embed in the text channel where command was issued. Save the message ID on the GuildQueue so future updates can edit it.
- `on_wavelink_track_end`: pull next track via `queue_manager.next()`, play it. If queue is empty:
  - If channel has autoplay playlist configured (24/7 + autoplay): re-resolve playlist, shuffle, queue 50 tracks, play next.
  - Else if channel has 24/7 on (no autoplay): stay connected, idle silently.
  - Else: schedule disconnect after 60s of idle.
- `on_wavelink_track_exception` / `on_wavelink_track_stuck`: log error, send notice to text channel, advance queue.
- `on_voice_state_update`: if bot is alone in voice channel for 60s AND 24/7 is NOT enabled for that channel, disconnect. If 24/7 IS enabled, stay.

## 8. Setup script

`scripts/setup_lavalink.sh`:

- Downloads latest Lavalink v4 release JAR to `lavalink/Lavalink.jar`
- Downloads LavaSrc plugin JAR to `lavalink/plugins/`
- Verifies Java 17+ is on PATH; errors clearly if not
- Idempotent (skips download if files already current version)
- Pin specific version numbers in the script — do NOT use "latest" tags blindly

## 9. Acceptance criteria

V1 is done when:

1. Bot starts, Lavalink subprocess starts, wavelink connects — verified by log line `Wavelink node connected: <name>`.
2. `/play never gonna give you up` plays the song in the user's voice channel.
3. `/play https://open.spotify.com/track/...` plays the resolved YouTube equivalent within 3s.
4. `/play https://open.spotify.com/playlist/...` (10+ tracks) imports all tracks, plays first, queues rest. Resolution completes in <30s for 100 tracks.
5. Now-playing embed posts on track start. All five buttons function. Loop button cycles through three modes with emoji change.
6. `/queue` shows current + queued tracks with positions and durations.
7. `/shuffle` reorders queue without interrupting current track.
8. `/loop track` repeats current track on end. `/loop queue` repeats whole queue. `/loop off` returns to normal advance.
9. Bot disconnects after 60s alone in voice channel — UNLESS 24/7 is enabled for that channel.
10. `/247 enabled:true` in a voice channel persists the setting; bot stays in channel even when empty. After bot restart, bot rejoins all 24/7 channels within 60s of cog load.
11. `/247 enabled:true autoplay_playlist:<spotify_url>`: when queue empties, bot re-shuffles and queues from the playlist automatically. Continues indefinitely.
12. `/247_status` shows all channels with 24/7 enabled in the current guild.
13. `Ctrl+C` on the bot host: bot logs off cleanly, Lavalink subprocess exits within 15s. No orphan Java processes.
14. If Lavalink fails to start, cog load raises a clear error, rest of Dungeon Keeper continues running.
15. If a YouTube track fails to load (e.g., region-blocked), bot logs the error and advances to next track without crashing.

## 10. Edge cases to handle

- **User runs `/play` while not in voice:** ephemeral "Join a voice channel first."
- **User runs `/play` while in different voice channel from bot:** ephemeral "I'm currently in #other-channel. Join me there or wait for the queue to finish."
- **Spotify playlist with >500 tracks:** truncate to 500, send embed warning "Playlist is X tracks; queued the first 500."
- **Spotify track with no ISRC and no obvious YouTube match:** skip, log warning, continue.
- **Bot restart with active queue:** queue is in-memory only — not persisted. After restart, queue is empty. (Persistence is v2.)
- **Voice channel deleted while bot is in it:** disconnect cleanly, clear queue. If 24/7 was enabled for that channel, also remove the setting from the store and log it.
- **Lavalink subprocess dies mid-playback:** log error, attempt single restart, if that fails, alert all active guilds and disconnect.
- **24/7 enabled on a channel the bot lacks Connect permission for:** log warning at rejoin time, leave the setting intact (permissions may be restored later), notify a configured mod log channel if available.
- **Autoplay playlist becomes private / deleted:** when re-resolution fails, bot pauses autoplay for that channel, posts a notice in the last-used text channel, but stays in voice (24/7 still active).
- **Multiple 24/7 channels in one guild:** bot can only be in one voice channel per guild. v1 limits 24/7 to ONE channel per guild — `/247 enabled:true` in a new channel disables it on the old one with a confirmation prompt.

## 11. Logging

Use Dungeon Keeper's existing logger. Music cog logs to a `music` child logger.

Levels:
- `INFO`: track start/end, queue add, voice connect/disconnect, Lavalink start/stop
- `WARNING`: track resolve failures, retries, alone-in-channel disconnect
- `ERROR`: Lavalink subprocess crash, wavelink disconnect, unhandled command exceptions

## 12. Open questions for implementer

1. Confirm Dungeon Keeper's existing cog discovery pattern — is it `bot.load_extension("cogs.music.music")` or auto-discovery? Adjust `__init__.py` accordingly.
2. Confirm where Dungeon Keeper expects logs to land — match that.
3. Confirm whether Dungeon Keeper has a startup health-check pattern. If yes, hook Lavalink readiness into it.

## 13. Future hooks (do not implement, but design for)

- **v2 — member quality scoring integration:** when a track ends naturally (not skipped), emit an event the Dungeon Keeper scoring module can subscribe to, with `{user_id, guild_id, duration_played_s}`. Don't build the listener in v1, but make sure the hook point exists.
- **v2 — persistent queues:** queue_manager state should be cleanly serializable to JSON for future SQLite persistence.
- **v2 — Apple Music/Deezer:** LavaSrc supports them; v1 just disables them in config. Re-enabling is a config change, not a code change.

## 14. Out-of-scope reminder

Do not implement:
- Lyrics
- Audio filters / EQ
- Vote-skip
- Per-user request limits
- Saved community playlists (named, shareable presets)
- Member quality scoring listener (only the emit hook)
- Multiple 24/7 channels per guild simultaneously (single-channel only in v1)
