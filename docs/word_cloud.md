# Word Cloud

> **Classification: Reference** — matches current behavior.

`/wordcloud` renders a picture of the words a channel has been using over a
window. It is **moderator-only** and replies **ephemerally**.

## Why it is mod-only

The command reads the message archive, which `docs/data_register.md` records as
full message content with indefinite retention. The privacy notice
(`manual.html` §Your Data & Privacy) already discloses that this archive is
"visible to admins and moderators through the dashboard", so a mod-gated,
ephemeral cloud shows that audience nothing they cannot already read. An open
member-facing version would change that disclosure, and would need a member
opt-out and a `data_register.md` decision; neither exists, because neither was
built.

**Read permission is the gate.** A moderator can cloud any channel they can
read, and no channel they cannot — checked with `permissions_for(invoker)` on
the named channel, and used to build the channel list for `everywhere`. There
is deliberately **no NSFW gate**: the reply never leaves the invoking
moderator, and an age-gated room is one they can already read.

**Private threads need their own check.** `Thread.permissions_for` only
inherits the parent channel's overwrites — it knows nothing about who was
invited — so a private thread the moderator was never added to would otherwise
come back "readable" and land in an `everywhere` cloud. Discord's own rule is
invitation *or* Manage Threads, and only the second half is knowable from the
cache, so `_readable_channel_ids` requires `manage_threads` for any private
thread. Archived threads are absent from `guild.threads` and so are never
clouded by `everywhere`; running the command *inside* a thread still clouds
that thread, since `interaction.channel` is proof of access.

## Command

`/wordcloud [window] [channel] [member] [everywhere] [preset] [color]`

| Option | Default | Notes |
|---|---|---|
| `window` | `24h` | `30m`, `6 hours`, `7d`. Two years is the ceiling. |
| `channel` | the current one | Any channel the invoker can read. |
| `member` | everyone | One person's words only. |
| `everywhere` | off | Every channel the invoker can read, instead of one. |
| `preset` | the guild dial | One of five visual styles. |
| `color` | by mood | `sentiment` or `palette`. |

## Two corpus paths

| Guild | Path | Window |
|---|---|---|
| `message_storage_level = all` | Archive SQL (`corpus.fetch_archive`) | full, back to the archive's start |
| anything else | `channel.history()`, stored nowhere | **clamped to 10 minutes** |

Only the home guild archives content; the other seven keep ids and timestamps
but no text. The reply always says which path ran, because "a quiet week" and
"this server keeps no message text" are different answers to an empty cloud —
`corpus.archive_has_content` is what tells them apart.

A live `everywhere` fans out over at most `LIVE_CHANNEL_FANOUT` (25) channels,
ranked by their most recent message. That ranking reads the archive's
timestamps, which exist in every guild regardless of storage level, so it works
precisely where the live path is needed.

## What is filtered, and why

Measured over seven days of home-guild traffic, the top raw tokens were `white`
(17,357) and `square` (11,861) — a bot's board art. With bot authors excluded,
`https`/`com`/`gifs`/`klipy` rose to replace them. So:

- **Bot authors excluded** via `core.bot_exclusion.bot_filter_clause` (~21% of
  stored volume). Naming an `author_id` overrides this — asking for one
  account's words means that account, bot or not.
- **Deleted messages excluded** (`deleted_at IS NULL`). The archive outlives
  Discord deletions by design; 40,431 home-guild rows carry the column. A cloud
  that ignored it would resurface words a member removed on purpose.
- **URLs, custom emoji, mentions, code fences and inline code stripped** before
  tokenising.
- **Typographic apostrophes folded to ASCII.** U+2019 outnumbers the ASCII
  apostrophe 185 to 73 in `don't` alone; unfolded, every contraction splits and
  the orphaned stem (`don`, `that`, `it`) walks past the stopword list. This was
  a real defect caught on live data, not a hypothetical.
- **Stopwords and words under 3 characters dropped.**

## Sentiment colouring

`messages.sentiment` is already populated at ingest, so each word can be tinted
by the mean sentiment of the messages it appeared in — warm for happier, cool
for unhappier, the preset's neutral stop for the middle. Averaged per
*occurrence*, matching the way the count sizes the word.

The live path has no scores. When mood colouring is asked for and no scores
exist, the cloud falls back to the preset palette and the reply says so.

## Dials

Dashboard → Configuration → Channels & Messages → **Word Cloud** (route id
`word-cloud`, `PUT /api/config/word-cloud`, admin-gated).

| Key | Default | Notes |
|---|---|---|
| `word_cloud_message_cap` | 12000 | Clamped to 100–12000 on save. A cap that bites is reported on the card — both corpus paths read `cap + 1` rows so "exactly full" can be told from "truncated". |
| `word_cloud_default_preset` | `midnight` | An unknown key degrades to the default rather than raising. |

Both are read **without** the legacy `guild_id=0` fallback, by the cog and by
`GET /api/config` alike. They are new keys, so a row at `0` could only ever be
the home guild's, and a second guild must not inherit it.

## Rendering

`wordcloud==1.9.6` (added to `requirements.txt` with this feature; no new
transitive deps — numpy, Pillow and matplotlib were already locked). It pulls
matplotlib for colormaps, so `render.py` sets `MPLCONFIGDIR` **before** the
import exactly as `services/activity_graphs` does — the unit runs
`ProtectHome=read-only` and matplotlib resolves the path at import time.

`pyplot` is never touched (`WordCloud.to_image()` returns a PIL image), so this
needs none of the serialisation `services/pyplot_lock` exists for. The render
runs in `asyncio.to_thread`.

## No new tables

The two dials are guild config rows. No per-user data is stored, so there is no
`docs/data_register.md` row to add.

## Tests

- `tests/test_word_cloud_logic.py` — window parsing, the 10-minute clamp, every
  stripping rule, apostrophe folding, stopwords, the cap, sentiment averaging,
  preset resolution and colour.
- `tests/test_word_cloud_corpus.py` — bot and deleted exclusion, guild and
  channel scoping, window boundaries, the author override, channel ranking.
