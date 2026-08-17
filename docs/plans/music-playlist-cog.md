# Music playlist cog — a watched channel becomes a rolling playlist

Decided with the user 2026-08-16: port Billy's **OpenMusicBot**
(`github.com/billydev-secret/OpenMusicBot`, 4,509 lines, last pushed
2026-03-29) into DK as a cog. It watches one channel, pulls music links out
of what people post, resolves them to Spotify tracks, dedupes, and keeps a
**rolling playlist of the last 30 songs**.

**Built 2026-08-17** — this plan is now history, kept for the sign-off
record and the platform scoping (YouTube/Apple Music) that was deliberately
deferred. The living reference is `docs/music_playlist_spec.md`; where this
plan and the code disagree, the code wins. The four open questions at the
end were resolved: albums/playlists skipped behind a default-off
`expand_albums` dial; parsing is Spotify + YouTube only; rolled-off history
rows are kept; source-message reactions are on.

## What Billy decided

| Question | Answer |
|---|---|
| Playlist shape | **Rolling last 30** — song 31 pushes the oldest out |
| Watched channels | **One**, picked on the dashboard |
| Message deleted | **Track leaves the playlist** |
| Whose account | **His personal Spotify**, revisit later; config must allow swapping the owning account without a schema change |
| Platforms | **Spotify build-now**; YouTube and Apple Music **scoped and costed**, approved separately |

## The blocker Billy has to clear himself

DK already has the entire Spotify user-OAuth path — `/spotify/authorize` and
`/spotify/callback` (`src/web_server/routes/spotify_oauth.py`), the refresh
token persisted as `spotify_bot_refresh_token`, and `_get_user_client()` in
`services/spotify_resolver.py:99` which refreshes, caches, and hands back a
spotipy client authed as him. It is load-bearing today at
`spotify_resolver.py:209` for reading his private playlists.

**The live token is read-only.** `spotify_oauth.py:30`:

    SPOTIFY_SCOPES = "playlist-read-private playlist-read-collaborative"

Writing needs `playlist-modify-public` and `playlist-modify-private`. No code
change makes the stored token write; the grant itself is the wrong shape.

So stage 0 is: widen the constant to

    playlist-read-private playlist-read-collaborative
    playlist-modify-private playlist-modify-public

— keeping both read scopes, or re-consent silently breaks private-playlist
reading — and then **Billy visits `/spotify/authorize` once more and clicks
through.** That is a step he performs, after the code ships and the service
restarts. Until he does, the feature is inert.

To make that failure legible instead of mysterious: the callback already
stores the granted scope as `spotify_bot_scope` (`spotify_oauth.py:152`), so
the service checks it before every write and the dashboard panel shows a
status chip — **Connected / Read-only, needs re-consent / Not connected**.
A missing scope reports itself in those words rather than surfacing as a bare
403 from Spotify.

## What ports, and what doesn't

Of 4,509 lines, roughly **930 are worth carrying** and they are already in
DK's shape — pure logic with tests.

**Port:**

| Source | Lines | Lands as |
|---|---|---|
| `message_parsing.py` (+ `tests/test_message_parsing.py`) | 121 | `music_playlist_logic.py` |
| `matching.py` (+ `tests/test_matching.py`) | 147 | `music_playlist_logic.py` |
| `message_processor.py` | 664 | `music_playlist_service.py` |
| `spotify_client.py:628-698` — `add_tracks_to_playlist` / `remove_tracks_from_playlist` | ~70 | grows `spotify_resolver.py` |

`matching.py` is the piece that earns the port: title cleaning that strips
"official video", "lyrics", "HD", "remaster", "live", "sped up", "slowed +
reverb"; artist inference from `Artist - Title`, `Title by Artist`, and the
`… - Topic` channel convention; a 0.72/0.28 title/artist blend with a
substring bonus and a mismatch penalty when one side says "live"/"remaster"/
"acoustic" and the other doesn't. That is tuned behavior, not code worth
retyping. **Both test files come across with it.**

**Do not port:**

| Source | Lines | Why |
|---|---|---|
| `discord_commands.py` | **1,231** | 20 slash commands. Straight against house rules — see below |
| `database.py` + `sql/schema.sql` | 761 | DK has migrations and `open_db`; re-express, don't import |
| `spotify_client.py` (the rest) | ~840 | Token machinery DK already has, better |
| `main.py`, `config/settings.py`, `logging_utils.py` | ~200 | DK has all three |

The 20 commands — `add`, `approve`, `channel`, `cleanup`, `help`, `history`,
`list`, `pause`, `playlist`, `reject`, `remove`, `resume`, `set`, `show`,
`spotify`, `status`, `summary`, `sync`, `ttl`, `unmatched` — are the single
biggest divergence from CLAUDE.md, which puts configuration on the dashboard
and nowhere else. Eighteen of them collapse into **one admin-gated dashboard
panel**. The two with a genuine member-facing job (`list`, `show`) collapse
into **one ephemeral panel**, not a family of subcommands.

## Shape

One listener on the watched channel. Per message:

1. **Parse** every URL out of the content (`extract_links`).
2. **Resolve** each to a Spotify track id — direct for `open.spotify.com/track`
   and `spotify:track:` URIs, by *search* for everything else.
3. **Dedupe** against the tracks currently in the window.
4. **Write** the survivors to the playlist, then **trim** back to 30.

Confidence gates step 2. Above `match_threshold` (OpenMusicBot's default is
**0.74**, and it's a dashboard dial here) the track is added silently; below
it, nothing is added and the item lands in an **unmatched review queue** with
its best candidate and score, for approve/reject. That design is right and
survives the port — only the queue moves, from `/unmatched` to the dashboard.

### Rolling 30 — the decisions it forces

A fixed window isn't just "delete the oldest"; it changes three things:

- **Dedupe scope is the window, not all time.** A song that rolled off months
  ago should be postable again. So the unique index is partial — on live rows
  only — and the DB keeps rolled-off rows as history rather than deleting them.
- **Album and playlist links have to be handled or they eat the window.**
  OpenMusicBot expands an album link into all its tracks
  (`message_processor.py:145-169`); one album post would flush 30 songs and
  wipe everyone else's. **Recommendation: skip album/playlist links entirely**
  (they aren't "a song someone posted"), behind a default-off `expand_albums`
  dial for when he wants it. **This one needs a yes/no from Billy.**
- **Trimming and deletion both remove**, so removals need a reason
  (`rolled_off` / `message_deleted` / `admin`) or the history is unreadable.

### Deletions

`on_raw_message_delete`, not `on_message_delete` — the raw event fires for
messages that were never in the bot's cache, which for an old post is most of
them.

One subtlety: two people can post the same song, and the playlist holds it
once. So a delete removes the track **only when no other live message still
references it** — otherwise deleting your post silently revokes someone
else's. Cheap to get right, ugly to discover in prod.

## Storage (migration 165)

Three tables. The plan guessed `164`; a parallel session (casino Mines)
merged ahead of this one and took it, so the built migration is **165** —
exactly the collision this note warned about.

- **`music_playlist_tracks`** — the window and its history. `guild_id`,
  `platform` (`'spotify'`, so the schema doesn't need rewriting when a second
  writer lands), `playlist_id`, `track_id`, `title`, `artist`, `source_url`,
  `channel_id`, `message_id`, **`added_by`**, `added_at`, `removed_at`,
  `removal_reason`. Partial unique index on
  `(guild_id, platform, playlist_id, track_id) WHERE removed_at IS NULL`.
- **`music_playlist_unmatched`** — the review queue. Source url, extracted
  title/channel, best candidate + confidence + reason, `posted_by`, status
  (`pending`/`approved`/`rejected`), reviewer, reviewed_at.
- **`music_playlist_messages`** — processed-message ledger, so a restart or a
  manual re-scan is idempotent.

Settings ride the config KV behind a `MusicPlaylistSettings` dataclass, the
way `CasinoSettings` does — no settings table: `enabled`, `channel_id`,
`playlist_id`, `window_size` (30), `match_threshold` (0.74), `expand_albums`
(false), `remove_on_delete` (true), `owner_account_label`. The owning Spotify
account is a *config* concern, not schema, which is what makes "revisit later"
cheap.

### Per-user data

`added_by` and `posted_by` name a member, so both tables need a row in
`docs/data_register.md` in the same commit, with an explicit purge decision.
**Recommendation: purge clears both** — there's no legal-claims or
integrity ground to preserve "who posted a song", and the playlist itself is
unaffected because the tracks are keyed by Spotify id, not by poster.

Lucky break: **`added_by` is already in `privacy_service.SUBJECT_ID_COLUMNS`**
(`privacy_service.py:272`), so the export sees `music_playlist_tracks` for
free. `posted_by` is **not** — either add it there, or name the column
`added_by` too and skip the change. Prefer the latter.

Member-facing collection also needs a line in the privacy notice —
`manual.html` §Your Data & Privacy.

## Dashboard panel

Route id `music-playlist` (bare feature name, per `docs/dashboard_ia.md`),
admin-gated, filed under the same heading as the other content features.
It is where the 18 admin commands go:

| Panel section | Replaces |
|---|---|
| Connection — scope chip, Connect/Re-consent button, owning account | `spotify` |
| Watch — channel picker, target playlist, enable, pause | `channel`, `set`, `playlist`, `pause`, `resume` |
| Behavior — window size, threshold, expand-albums, remove-on-delete | `ttl`, `set` |
| Window — the live 30, newest first, with a remove button per row | `list`, `show`, `remove` |
| Review queue — pending unmatched, candidate + score, approve/reject | `unmatched`, `approve`, `reject` |
| History — rolled-off and removed tracks, with reason | `history`, `summary`, `status` |
| Maintenance — re-scan channel, reconcile with Spotify | `sync`, `cleanup`, `add` |

Both tables mount through `mountAsync` and render via the shared `table.js`,
which escapes every cell.

### Help section

The upstream `help` command doesn't just disappear — it becomes a proper
manual section, wired the way every other panel's help is:

- A **`manual.html` section** covering both audiences: for members, how the
  channel works (post a link, what ✅/🔁/❓ mean, why a song can land in
  review, the rolling-30 rule); for admins, every dial on the panel plus the
  re-consent flow and what the Read-only chip means.
- An entry in **`help-sections.js`** (`page: "help-music-playlist"`, anchor to
  the new heading, keywords) — that file is the single source of truth for
  Help nav, and a missing anchor shows a visible "not found" instead of
  drifting silently.
- The panel's nav entry carries `help: "help-music-playlist"` so the ? link
  from the config page lands on the right section.

## Member-facing Discord surface

Deliberately thin: **one ephemeral panel** — what's in the window, what you
posted, and what of yours is sitting in review. No admin dials, no
subcommands. Whether the bot reacts on the source message (✅ added, 🔁
duplicate, ❓ queued) is worth doing and cheaper than any of it —
**recommend yes**, as the only non-ephemeral feedback.

## The other two platforms

They are not three equivalent bullets. Spotify is a week of work on rails DK
already owns; YouTube is a second OAuth story with a real quota model; Apple
Music has no server-side auth flow at all.

### The writer abstraction — worth building, but not yet

The obvious shape is `parse → resolve → dedupe → write to N destinations`,
with a `PlaylistWriter` protocol (`add(track)`, `remove(track)`, `list()`) per
platform. It is the right end state and the schema above is already built for
it (`platform` column, per-platform rows).

But **building the abstraction now, against one implementation, is guessing.**
The three platforms disagree on the thing an abstraction has to hide: Spotify
takes a stable track id; YouTube takes a video id you must *search* for and
can only insert 200 times a day; Apple Music takes a catalog id and can't be
written from a server at all without a browser handshake. A protocol drawn
around Spotify alone will be wrong for at least one of them.

**Recommendation: ship Spotify with the write calls behind a thin
`SpotifyPlaylistWriter` class and the `platform` column in place, and extract
the protocol when the second writer actually lands.** The cost of extracting
later is a few hours; the cost of guessing wrong now is a refactor plus a
migration.

### YouTube — feasible, second OAuth, quota is real but not the binding constraint

Writing a YouTube playlist means YouTube Data API v3 (`playlists.insert`,
`playlistItems.insert`) with Google OAuth on the `youtube` scope — a second
consent flow, a second stored token, a Google Cloud project.

Quota, verified against Google's current published table:

| Call | Cost |
|---|---|
| `playlistItems.insert` | 50 units |
| `playlistItems.delete` | 50 units |
| `playlistItems.list` | 1 unit |
| `videos.list` | 1 unit |
| `search.list` | **its own bucket, ~100 calls/day** |

Default allocation is **10,000 units/day**, plus separate 100-call daily
buckets for `search.list` and `videos.insert`. **This is a change from what
was assumed:** since a June 2026 granular-quota change, `search.list` no
longer draws 100 units from the shared pool — it bills to its own capped
bucket. So searching doesn't starve the inserts any more; it just runs out
on its own at ~100/day.

Doing the arithmetic at DK's actual scale: a rolling window in steady state
costs one insert **and** one delete per song, 100 units — so **~100 songs/day**
before the pool is gone, and ~100 Spotify→YouTube resolutions/day before
search is gone. A music channel does maybe 5–20 songs a day. **Quota is not
the binding constraint at this scale** — it needs accounting and a cached
resolution table so a re-scan doesn't re-search, not a redesign.

The real cost is the OAuth consent screen. An External app left in **Testing**
status issues refresh tokens that **expire after 7 days** — the token would
die every week. Fixing that means publishing to Production; the `youtube`
scope is sensitive, so expect a verification step (and, unverified, a warning
screen and a 100-user cap — irrelevant for one user, but it has to be walked
through). **Verify the exact verification path at build time; it moves.**

**Estimated cost: ~1.5× the Spotify stage.** Most of it is auth and ops, not
logic.

### Apple Music — recommend against

Verified, and it holds:

- Needs a **paid Apple Developer Program membership** to make the MusicKit
  signing key that signs the developer token.
- Creating a playlist needs a **Music User Token**, obtainable *only* through
  **MusicKit JS in a browser**, on a device signed into an active Apple Music
  subscription. There is no server-side redirect flow like Spotify's or
  Google's — the dashboard would need its own MusicKit JS page.
- That token **expires after 6 months with no refresh mechanism**. Billy
  re-does the browser handshake twice a year, forever, or the feature stops.
- Playlists created via `POST /v1/me/library/playlists` land in his **private
  library** with `isPublic: false`, and **there is no API call that makes one
  shareable.** The share link requires a `globalId` that only appears after
  tapping Share (or "Show on My Profile and in Search") **by hand in the Music
  app**.

That last point is the one that matters: the product here is *a link you drop
in the server so people can listen*. Apple Music can't produce that link
programmatically. For a single long-lived rolling playlist it's a one-time
manual tap, so it isn't fatal — but paired with a browser-only auth handshake
every six months and an annual developer fee, **the recommendation is to drop
Apple Music** and revisit only if Apple ships a server-side flow.

**Estimated cost if built anyway: ~2.5× the Spotify stage**, and it is the
only one of the three with recurring manual upkeep.

## Stages

Each is a commit with its tests, per CLAUDE.md.

0. **Scope widening** — `SPOTIFY_SCOPES` grows the two modify scopes;
   scope-check helper + the dashboard status chip. Tests: scope parsing,
   the read-only case reporting itself as read-only. **Then Billy re-consents
   at `/spotify/authorize`.** Nothing after this works until he does.
1. **Logic + storage** — `music_playlist_logic.py` (parsing + matching,
   ported with both upstream test files); migration 165; store functions.
   Tests: the ported suites, plus window trimming, dedupe-within-window,
   and rolled-off-then-reposted.
2. **Service + writes** — `music_playlist_service.py` (the pipeline);
   `add_tracks_to_playlist` / `remove_tracks_from_playlist` on the resolver.
   Tests: happy path, below-threshold → queue, duplicate, album-link skip,
   delete-with-another-live-referrer, missing-scope, Spotify 403/429.
3. **Cog** — listener, raw-delete handler, the one ephemeral member panel,
   optional source reactions.
4. **Dashboard + docs** — the panel and its routes, `data_register.md` rows,
   `manual.html` (member + admin help section **and** the privacy-notice
   line), the `help-sections.js` entry + nav `help:` mapping,
   `INDEX.md` classification.

Stages 1–2 are the port. Stage 4 is the biggest single chunk, because it
absorbs 1,231 lines of slash commands.

## Explicitly out (this round)

Apple Music (recommended against above). YouTube as a write destination
(scoped, approved separately). Several channels or several playlists — he
picked one of each. Per-member posting limits. Any slash command beyond the
single ephemeral panel. The extracted `PlaylistWriter` protocol, until a
second writer exists to shape it.

## Open questions for sign-off

1. **Album and playlist links — skip, or expand?** Recommendation: skip,
   with a default-off dial. Expanding flushes the 30-song window on one post.
2. **New parsing beyond Spotify + YouTube.** Billy asked for Apple Music and
   SoundCloud *links* to resolve into Spotify tracks. Upstream handles
   Spotify and YouTube only, so both are net-new parsing — cheap (a URL
   pattern and a title source each) but not free, and SoundCloud titles are
   noisier than YouTube's, so more of them will land in the review queue.
   In scope for stage 1?
3. **Rolled-off tracks: keep the history rows forever, or age them out?**
   They're small, and they're what makes "what did we listen to in July"
   answerable — but they're also per-user data with no retention story yet.
4. **Reactions on the source message** (✅ / 🔁 / ❓) — recommended, confirm.
