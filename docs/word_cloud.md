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
read, and no channel they cannot. Every gate lives in `logic.plan_scope`, which
takes plain data and returns a `Scope` or a `Refusal`, so each denial is
asserted directly rather than through a Discord mock. The order is: in a guild,
then staff, then able to read what was asked for — a non-moderator gets the
same wording whether or not the channel exists. `cogs/word_cloud_cog._can_read`
is the single definition of "may read", used both for a named channel and for
every candidate in the `everywhere` fan-out.

There is deliberately **no NSFW gate**: the reply never leaves the invoking
moderator, and an age-gated room is one they can already read.

`everywhere` and `channel` overlap. `everywhere` wins, and the card says so
rather than discarding the picked channel silently.

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
| `message_storage_level = all`, archive has text | Archive SQL (`corpus.fetch_archive`) | full, back to the archive's start |
| `message_storage_level = all`, archive empty | live, `LIVE_EMPTY_ARCHIVE` | **clamped to 10 minutes** |
| anything else | live, `LIVE_NO_STORAGE` | **clamped to 10 minutes** |

The two live reasons carry **different copy**. A guild that archives content but
has nothing stored yet (newly enabled, or just purged) is told "nothing is
stored for this server yet" — telling it that it "doesn't keep message text"
would be false and would contradict its own privacy notice.

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

The archive predicate is built by
`services/message_search_service.build_where` from a `MessageFilters`, not by
hand: it is the repo's one description of what a filtered read of `messages`
means, and a second one here would let the two archive readers drift on
questions like what "exclude bots" covers. `corpus.recent_channel_ids` is
deliberately *not* built on it — that ranks rooms by last traffic and must
count every row, including the bot-authored, deleted and content-free ones a
filtered read drops.

- **Bot authors excluded** (~21% of stored volume) by `build_where`'s own rule.
  Naming an author turns that exclusion off, which is the behaviour wanted:
  asking for one account's words means that account, bot or not.
- **Deleted messages excluded** (`deleted=DELETED_LIVE`). The archive outlives
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

## Layering

`logic.py` owns the dial keys and `clamp_cap`, the window parsing and clamp,
tokenising and counting, `plan_scope`, and every line of reply copy.
`embeds.py` builds the card and escapes the member's display name — an
unescaped `__Robin__` would reformat the description. `corpus.py` reads,
`render.py` draws, `presets.py` styles (its fonts are keys into
`quote_renderer.FONT_STYLES` rather than a second catalogue of the same five
files). The cog resolves Discord objects, calls those, and sends.

The cap's floor and ceiling exist once, in `logic.py`. `GET /api/config`
returns them alongside the preset list so the dashboard panel renders its own
bounds instead of re-typing the numbers, and the panel uses `selectValueOrAdd`
so a stored preset key this build doesn't know is never silently overwritten.

## No new tables

The two dials are guild config rows. No per-user data is stored, so there is no
`docs/data_register.md` row to add.

## Tests

- `tests/test_word_cloud_logic.py` — window parsing, the 10-minute clamp, every
  stripping rule, apostrophe folding, stopwords, the cap, sentiment averaging,
  preset resolution and colour.
- `tests/test_word_cloud_corpus.py` — bot and deleted exclusion, guild and
  channel scoping, window boundaries, the author override, channel ranking.
